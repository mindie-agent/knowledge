"""[Role] Authoritative SQLite state opener and schema integrity guard.

[Invariants]
- An existing database is never silently recreated or truncated on corruption;
  missing tables, missing columns, 0-byte files, or leftover WAL/SHM/journal
  files without the main database raise ValueError immediately.
- A created-but-not-yet-locked `.init.lock` is not authority evidence. The
  initializer record is persisted only after the database exists and its
  initialization/commit/validation succeeded, and only when the validated DB
  has no durable record: a fresh initialization, a legacy DB without a record,
  or repair after a failed record write. Existing records are not rewritten
  or fsynced on every open; their PID/time fields are diagnostic, not liveness.
- Once a database file exists, 0-byte or schema damage remains fail-closed even
  when no record was persisted; it is never reclassified as first use.
- The first init record is authority evidence: after a validated open, failure
  to persist it is raised rather than swallowed as diagnostic-only noise.
- Read-only probes (`initialize_missing=False`) never create directories, lock
  files, records, or empty SQLite files when the database has never been used.

[Timeouts & Locks]
- Cross-process initialization is serialized by `StartLock(<db>.init.lock)` with
  a bounded `timeout` (default 10.0s) and SQLite `timeout` (default 5.0s) in WAL mode.

[Side-Effect Policy]
- Local filesystem and SQLite state only; zero network or subprocess execution.
"""
from functools import lru_cache
import errno
import os
import sqlite3

from .loop.locks import StartLock
from .filesystem import filesystem_path, sqlite_file_uri


def require_columns(db, required):
    for table, columns in required.items():
        present = {row[1] for row in sqlite3.Connection.execute(db, f'PRAGMA table_info({table})')}
        if not set(columns) <= present:
            raise ValueError(f'authoritative state table {table} is missing or incomplete; state was not rebuilt')


@lru_cache(maxsize=None)
def _declared_columns(initialize):
    db = sqlite3.connect(':memory:')
    try:
        initialize(db)
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {table: {row[1] for row in db.execute(f'PRAGMA table_info({table})')} for table in tables}
    finally:
        db.close()


def require_schema(db, initialize):
    require_columns(db, _declared_columns(initialize))


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        for parent in path.parents:
            if parent.exists():
                if not parent.is_dir():
                    raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(parent)) from None
                return False
        return False


class OwnedConnection(sqlite3.Connection):
    def assert_authority(self, *, full=False):
        if hasattr(self, '_authority_path') and not self._authority_path.exists():
            raise FileNotFoundError(str(self._authority_path))


def open_database(path, *, schema, required, initialize, residue=(), validate=None,
                  initialize_missing=True, validate_current=None, timeout=10.0):
    """Initialize only a demonstrably new store; preserve every damaged file."""
    path = filesystem_path(path)
    lock_path = path.with_name(path.name + '.init.lock')
    leftovers = (*residue, *(path.with_name(path.name + suffix) for suffix in ('-wal', '-shm', '-journal')))
    if (not initialize_missing and not _present(path) and not _present(lock_path)
            and not any(_present(p) for p in leftovers)):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = StartLock(lock_path)
    # No record before the database exists: only a validated DB earns durable
    # authority evidence. Generic StartLock callers keep their own diagnostics.
    lock.acquire(wait=timeout, write_record=False)
    db = None
    try:
        if path.is_symlink():
            raise ValueError('authoritative state must be a regular local file')
        fresh = not path.exists()
        # `fresh` means the path is absent, so only prior evidence/residue can
        # distinguish first use from lost authority. Once a file exists, 0-byte
        # or schema damage stays fail-closed even without a persisted record.
        if fresh and (lock.prior_record or any(p.exists() or p.is_symlink() for p in leftovers)):
            raise ValueError('authoritative state database is missing; existing material and receipts were preserved')
        if fresh and not initialize_missing:
            return None
        if not fresh and (not path.is_file() or path.stat().st_size == 0):
            raise ValueError('authoritative state database is empty or invalid; state was not rebuilt')
        db = sqlite3.connect(path if fresh else sqlite_file_uri(path) + '?mode=rw',
                             uri=not fresh, check_same_thread=False, factory=OwnedConnection,
                             timeout=5.0 if timeout is None else timeout)
        db._authority_path = path
        db.row_factory = sqlite3.Row
        if fresh:
            initialize(db)
            db.commit()
        require_columns(db, required)
        if validate is not None:
            validate(db)
        db.execute('PRAGMA journal_mode=WAL')
        if os.environ.get('MINDIE_DISABLE_FSYNC') == '1':
            db.execute('PRAGMA synchronous=OFF')
        if initialize_missing and not lock.prior_record:
            # A validated DB without a durable record is the only case that
            # needs the authority write: fresh initialization, a legacy DB, or
            # repair after a failed record write. Existing records are not
            # refreshed; PID/time are diagnostic, not liveness.
            lock.persist_record()
        return db
    except BaseException:
        if db is not None:
            db.close()
        raise
    finally:
        lock.release()
