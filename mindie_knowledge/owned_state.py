"""Open authoritative SQLite state without turning damage into first use."""
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
    except FileNotFoundError as missing:
        for parent in path.parents:
            if parent.exists():
                if not parent.is_dir():
                    raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(parent)) from None
                return False
            if parent.is_symlink():
                raise missing from None
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
    had_lock = _present(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = StartLock(lock_path)
    lock.acquire(wait=timeout)
    db = None
    try:
        if path.is_symlink():
            raise ValueError('authoritative state must be a regular local file')
        fresh = not path.exists()
        if fresh and (had_lock or any(p.exists() or p.is_symlink() for p in leftovers)):
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
        return db
    except BaseException:
        if db is not None:
            db.close()
        raise
    finally:
        lock.release()
