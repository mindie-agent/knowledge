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
- Cross-process initialization is serialized by `StartLock(<db>.init.lock)` and
  coordinated around one caller `deadline` (default `timeout=10.0s`) with SQLite
  `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000` (`5.0s` lock floor).
- `open_database` sets `PRAGMA busy_timeout=5000` once at initialization without
  dynamic per-query overrides. Bounded callers with remaining budget below
  `SQLITE_BUSY_FLOOR_SECONDS` (`5.0s`) fail closed before acquiring `StartLock`
  or opening SQLite rather than shrinking `busy_timeout` or exceeding `deadline`.

[Side-Effect Policy]
- Local filesystem and SQLite state only; zero network or subprocess execution.
"""
import errno
import math
import os
import sqlite3
import time

from .loop.locks import StartLock
from .filesystem import filesystem_path, sqlite_file_uri

SQLITE_BUSY_TIMEOUT_MS = 5000
SQLITE_BUSY_FLOOR_SECONDS = SQLITE_BUSY_TIMEOUT_MS / 1000.0


class SqliteBudgetExhausted(sqlite3.OperationalError):
    """Caller deadline cannot cover the fixed 5000 ms SQLite lock policy."""


def sqlite_busy_timeout_ms(remaining=None):
    """Return the fixed 5000 ms PRAGMA busy_timeout when budget covers the floor.

    ``None`` remaining keeps the 5000 ms authority policy. Any remaining caller
    budget below ``SQLITE_BUSY_FLOOR_SECONDS`` (5.0 s) returns ``None`` so the
    caller fails closed before entering SQLite rather than shrinking
    ``busy_timeout`` dynamically or waiting past its deadline.
    """
    if remaining is None:
        return SQLITE_BUSY_TIMEOUT_MS
    if type(remaining) not in (int, float) or not math.isfinite(remaining):
        raise ValueError("SQLite remaining budget must be a finite number")
    if remaining < SQLITE_BUSY_FLOOR_SECONDS:
        return None
    return SQLITE_BUSY_TIMEOUT_MS


def _check_open_floor(open_deadline, stage):
    if open_deadline is None:
        return
    remaining = open_deadline - time.monotonic()
    if sqlite_busy_timeout_ms(remaining) is None:
        raise SqliteBudgetExhausted(
            f'SQLite caller budget ({remaining:.3f}s) is below the '
            f'{SQLITE_BUSY_FLOOR_SECONDS:.1f}s lock floor before {stage}'
        )


def _cleanup_uncommitted_fresh(path):
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = path if not suffix else path.with_name(path.name + suffix)
        try:
            candidate.unlink(missing_ok=True)
        except TimeoutError:
            raise
        except OSError:
            pass


def _report_authority_violation(reason_code, exc):
    try:
        from .loop.dfx import record_contract_violation
        record_contract_violation(
            'knowledge.owned_state',
            stage='open_database',
            contract_name='owned_sqlite_authority',
            reason_code=reason_code,
            exception=exc,
        )
    except TimeoutError:
        raise
    except Exception:
        pass


def require_columns(db, required, *, open_deadline=None):
    for table, columns in required.items():
        _check_open_floor(open_deadline, f'schema read ({table})')
        present = {row[1] for row in sqlite3.Connection.execute(db, f'PRAGMA table_info({table})')}
        if not set(columns) <= present:
            exc = ValueError(f'authoritative state table {table} is missing or incomplete; state was not rebuilt')
            _report_authority_violation('missing_required_columns', exc)
            raise exc


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
    def assert_authority(self, *, full=False, trusted=False):
        """Prove the opened authority still exists before a new transaction.

        ``trusted`` lets an owner that already holds its process lock skip a
        redundant lstat; the owning ``Store`` checks the first transaction of
        each outer write instead of every nested frame. External callers keep
        the filesystem check so damage remains visible.
        """
        if trusted:
            return
        if hasattr(self, '_authority_path') and not self._authority_path.exists():
            raise FileNotFoundError(str(self._authority_path))


def open_database(path, *, schema, required, initialize, residue=(), validate=None,
                  initialize_missing=True, validate_current=None, timeout=10.0,
                  deadline=None):
    """Initialize only a demonstrably new store; preserve every damaged file."""
    path = filesystem_path(path)
    lock_path = path.with_name(path.name + '.init.lock')
    leftovers = (*residue, *(path.with_name(path.name + suffix) for suffix in ('-wal', '-shm', '-journal')))
    if (not initialize_missing and not _present(path) and not _present(lock_path)
            and not any(_present(p) for p in leftovers)):
        return None
    open_deadline = deadline
    if open_deadline is None and timeout is not None:
        if type(timeout) not in (int, float) or not math.isfinite(timeout):
            raise ValueError('SQLite open timeout must be a finite number')
        if sqlite_busy_timeout_ms(timeout) is None:
            raise SqliteBudgetExhausted(
                f'SQLite caller budget ({float(timeout):.3f}s) is below the '
                f'{SQLITE_BUSY_FLOOR_SECONDS:.1f}s lock floor'
            )
        open_deadline = time.monotonic() + float(timeout)
    if open_deadline is not None:
        remaining = open_deadline - time.monotonic()
        if sqlite_busy_timeout_ms(remaining) is None:
            raise SqliteBudgetExhausted(
                f'SQLite caller budget ({remaining:.3f}s) is below the '
                f'{SQLITE_BUSY_FLOOR_SECONDS:.1f}s lock floor'
            )
        lock_wait = max(0.0, remaining - SQLITE_BUSY_FLOOR_SECONDS)
    else:
        lock_wait = None
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = StartLock(lock_path)
    # No record before the database exists: only a validated DB earns durable
    # authority evidence. Generic StartLock callers keep their own diagnostics.
    lock.acquire(wait=lock_wait, write_record=False)
    db = None
    clean_fresh_attempt = False
    fresh_committed = False
    try:
        if path.is_symlink():
            exc = ValueError('authoritative state must be a regular local file')
            _report_authority_violation('symlink_authority_path', exc)
            raise exc
        fresh = not path.exists()
        # `fresh` means the path is absent, so only prior evidence/residue can
        # distinguish first use from lost authority. Once a file exists, 0-byte
        # or schema damage stays fail-closed even without a persisted record.
        if fresh and (lock.prior_record or any(p.exists() or p.is_symlink() for p in leftovers)):
            exc = ValueError('authoritative state database is missing; existing material and receipts were preserved')
            _report_authority_violation('missing_database_with_residue', exc)
            raise exc
        if fresh and not initialize_missing:
            return None
        if not fresh and (not path.is_file() or path.stat().st_size == 0):
            exc = ValueError('authoritative state database is empty or invalid; state was not rebuilt')
            _report_authority_violation('empty_or_invalid_database', exc)
            raise exc
        clean_fresh_attempt = fresh
        _check_open_floor(open_deadline, 'connect')
        db = sqlite3.connect(path if fresh else sqlite_file_uri(path) + '?mode=rw',
                             uri=not fresh, check_same_thread=False, factory=OwnedConnection,
                             timeout=SQLITE_BUSY_FLOOR_SECONDS)
        db._authority_path = path
        db.row_factory = sqlite3.Row
        if open_deadline is not None:
            db.set_progress_handler(lambda: int(time.monotonic() >= open_deadline), 1000)
        db.execute(f'PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}')
        if fresh:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute(f'PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}')
            initialize(db)
            if open_deadline is not None and time.monotonic() >= open_deadline:
                raise SqliteBudgetExhausted('SQLite open exceeded caller deadline')
            db.commit()
        require_columns(db, required, open_deadline=None if fresh else open_deadline)
        if validate is not None:
            if not fresh:
                _check_open_floor(open_deadline, 'validation')
            validate(db)
        if not fresh:
            _check_open_floor(open_deadline, 'journal_mode check')
            mode = db.execute('PRAGMA journal_mode').fetchone()
            if not mode or str(mode[0]).lower() != 'wal':
                _check_open_floor(open_deadline, 'WAL transition')
                db.execute('PRAGMA journal_mode=WAL')
        # Keep the fixed 5000 ms policy explicit after initialization/validation.
        db.execute(f'PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}')
        if os.environ.get('MINDIE_DISABLE_FSYNC') == '1':
            db.execute('PRAGMA synchronous=OFF')
        if open_deadline is not None and time.monotonic() >= open_deadline:
            raise SqliteBudgetExhausted('SQLite open exceeded caller deadline')
        fresh_committed = fresh
        if initialize_missing and not lock.prior_record:
            # A validated DB without a durable record is the only case that
            # needs the authority write: fresh initialization, a legacy DB, or
            # repair after a failed record write. Existing records are not
            # refreshed; PID/time are diagnostic, not liveness.
            lock.persist_record()
        if open_deadline is not None:
            db.set_progress_handler(None, 0)
        return db
    except sqlite3.OperationalError as exc:
        if db is not None:
            if open_deadline is not None:
                db.set_progress_handler(None, 0)
            db.close()
        if clean_fresh_attempt and not fresh_committed:
            _cleanup_uncommitted_fresh(path)
        if (
            open_deadline is not None
            and not isinstance(exc, SqliteBudgetExhausted)
            and 'interrupted' in str(exc).lower()
        ):
            raise SqliteBudgetExhausted('SQLite open exceeded caller deadline') from exc
        raise
    except BaseException:
        if db is not None:
            if open_deadline is not None:
                db.set_progress_handler(None, 0)
            db.close()
        if clean_fresh_attempt and not fresh_committed:
            _cleanup_uncommitted_fresh(path)
        raise
    finally:
        lock.release()
