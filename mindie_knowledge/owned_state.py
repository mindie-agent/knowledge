"""Open authoritative SQLite state without turning damage into first use.

The sibling marker records that this database has been initialized. It owns no
business data and cannot repair a missing database. Existing schema is checked
before any CREATE, so lost receipt tables never become permission to replay.
"""
from functools import lru_cache
import errno
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading

from .loop.locks import StartLock
from .filesystem import filesystem_path, sqlite_file_uri

_SQL_TOKENS = re.compile(r'''--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^]]*\]|[A-Za-z_][A-Za-z_0-9]*|[^\s]''', re.S)


def _sql_contract(sql):
    if sql is None:
        return None
    # Ignore formatting/comments, retaining literal bytes and predicate
    # operators. Lowercasing the whole SQL would hide changed string values.
    return tuple(token if token[0] in "'\"`[" else token.casefold()
                 for token in _SQL_TOKENS.findall(sql)
                 if token != ';' and not token.startswith(('--', '/*')))


def require_columns(db, required):
    for table, columns in required.items():
        present = {row[1] for row in sqlite3.Connection.execute(db, f'PRAGMA table_info({table})')}
        if not set(columns) <= present:
            raise ValueError(f'authoritative state table {table} is missing or incomplete; state was not rebuilt')


def _table_contract(db, table):
    # cid is not a contract: a valid older store may contain additional columns.
    exec_sql = sqlite3.Connection.execute
    columns = {row[1]: tuple(row[1:]) for row in exec_sql(db, f'PRAGMA table_info({table})')}
    indexes = {}
    for row in exec_sql(db, f'PRAGMA index_list({table})'):
        name = row[1]
        sql = exec_sql(db, "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        indexes[name] = (tuple(row[2:]), tuple(tuple(value[2:]) for value in
                         exec_sql(db, f'PRAGMA index_xinfo({name})')), _sql_contract(sql[0]) if sql else None)
    foreign = tuple(tuple(row) for row in exec_sql(db, f'PRAGMA foreign_key_list({table})'))
    kind = exec_sql(db, 'SELECT type,sql FROM sqlite_master WHERE name=?', (table,)).fetchone()
    autoincrement = kind is not None and 'autoincrement' in (_sql_contract(kind[1]) or ())
    return columns, indexes, foreign, kind[0] if kind else None, autoincrement


def _master_snapshot(db, tables=None):
    rows = sqlite3.Connection.execute(
        db, "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if tables is not None:
        tables = set(tables)
        rows = [row for row in rows if row[2] in tables]
    return frozenset((row[0], row[1], row[2], _sql_contract(row[3])) for row in rows)


@lru_cache(maxsize=None)
def _declared_master(initialize):
    db = sqlite3.connect(':memory:')
    try:
        initialize(db)
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        contracts = {table: _table_contract(db, table) for table in tables}
        return contracts, _master_snapshot(db, tables)
    finally:
        db.close()


def _declared_contract(initialize):
    return _declared_master(initialize)[0]


def require_schema(db, initialize):
    """Check types, null/default/PK contracts and their real index definitions."""
    cookie = sqlite3.Connection.execute(db, 'PRAGMA schema_version').fetchone()[0]
    verified = getattr(db, '_verified_schemas', None)
    if verified is not None and verified.get(initialize) == cookie:
        return
    contracts, expected_master = _declared_master(initialize)
    if _master_snapshot(db, contracts) != expected_master:
        for table, expected in contracts.items():
            actual = _table_contract(db, table)
            if (actual[3] != 'table' or any(actual[0].get(name) != value for name, value in expected[0].items())
                    or actual[1] != expected[1] or actual[2] != expected[2]
                    or expected[4] and not actual[4]):
                raise ValueError(f'authoritative state table {table} schema or constraints are invalid; state was not rebuilt')
    if verified is not None:
        verified[initialize] = cookie


def _file_stat(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError('authoritative state and ownership marker must remain regular files')
    return value


def _file_identity(path):
    value = _file_stat(path)
    return value.st_dev, value.st_ino


def _present(path):
    try:
        path.lstat()
    except FileNotFoundError as missing:
        # Windows also reports ENOENT for a child of a regular file. Confirm
        # that the closest existing ancestor can contain a missing path before
        # treating this as a never-initialized store. Other I/O errors escape.
        for parent in path.parents:
            try:
                mode = parent.stat().st_mode
            except FileNotFoundError:
                try:
                    parent.lstat()
                except FileNotFoundError:
                    continue
                raise missing from None  # Existing but unusable, e.g. a dangling directory symlink.
            if not stat.S_ISDIR(mode):
                raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(parent)) from None
            break
        else:
            raise missing  # No accessible ancestor established first-use absence.
        return False
    return True  # ENOTDIR/permission/I/O errors must not look like first use.


def _marker_bytes(marker, expected):
    descriptor = os.open(marker, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode) or source.read(len(expected) + 1) != expected:
            raise ValueError('authoritative state ownership marker is invalid')


class OwnedConnection(sqlite3.Connection):
    """Keep a cached connection bound to the same live authority files.

    Check cheap identities and the SQLite schema cookie at each operation.
    Structural introspection runs only after a schema change. Close/rollback
    remain available after authority loss so cleanup cannot hide the cause.
    """
    def bind_authority(self, path, marker, expected, initialize, validate_current=None):
        if not hasattr(self, '_verified_schemas'):
            self._verified_schemas = {}
        self._authority = path, marker, expected, initialize, validate_current
        p_stat, m_stat = _file_stat(path), _file_stat(marker)
        self._authority_identity = (p_stat.st_dev, p_stat.st_ino), (m_stat.st_dev, m_stat.st_ino)
        self._marker_sig = None
        self._validated_data_sig = None
        self._authority_lock = threading.RLock()
        self._validating_authority = False
        self._schema_cookie = sqlite3.Connection.execute(self, 'PRAGMA schema_version').fetchone()[0]
        self._verified_schemas[initialize] = self._schema_cookie
        self.assert_authority(full=True)

    def assert_authority(self, *, full=False):
        if not hasattr(self, '_authority'):
            return
        with self._authority_lock:
            if self._validating_authority:
                return
            path, marker, expected, initialize, validate_current = self._authority
            p_stat, m_stat = _file_stat(path), _file_stat(marker)
            if ((p_stat.st_dev, p_stat.st_ino), (m_stat.st_dev, m_stat.st_ino)) != self._authority_identity:
                raise ValueError('authoritative state identity changed; cached state cannot authorize further work')
            if p_stat.st_size == 0:
                raise ValueError('authoritative state database became empty; cached state cannot authorize further work')
            m_sig = (m_stat.st_dev, m_stat.st_ino, m_stat.st_mtime_ns, m_stat.st_size)
            if full or m_sig != self._marker_sig:
                if m_stat.st_size != len(expected):
                    raise ValueError('authoritative state ownership marker is invalid')
                _marker_bytes(marker, expected)
                self._marker_sig = m_sig
            if not full and self.in_transaction:
                return
            cookie = sqlite3.Connection.execute(self, 'PRAGMA schema_version').fetchone()[0]
            self._validating_authority = True
            try:
                if cookie != self._schema_cookie:
                    self._verified_schemas.clear()
                    require_schema(self, initialize)
                    self._schema_cookie = cookie
                    self._validated_data_sig = None
                if validate_current is not None:
                    data_ver = sqlite3.Connection.execute(self, 'PRAGMA data_version').fetchone()[0]
                    data_sig = (p_stat.st_mtime_ns, p_stat.st_size, cookie, data_ver)
                    if full or data_sig != self._validated_data_sig:
                        validate_current(self)
                        self._validated_data_sig = data_sig
            finally:
                self._validating_authority = False

    def execute(self, *args, **kwargs):
        if not hasattr(self, '_authority'):
            return super().execute(*args, **kwargs)
        self.assert_authority()
        with self._authority_lock:
            self._validating_authority = True
            try:
                return super().execute(*args, **kwargs)
            finally:
                self._validating_authority = False

    def executemany(self, *args, **kwargs):
        if not hasattr(self, '_authority'):
            return super().executemany(*args, **kwargs)
        self.assert_authority()
        with self._authority_lock:
            self._validating_authority = True
            try:
                return super().executemany(*args, **kwargs)
            finally:
                self._validating_authority = False

    def executescript(self, *args, **kwargs):
        if not hasattr(self, '_authority'):
            return super().executescript(*args, **kwargs)
        self.assert_authority(full=True)
        self._validated_data_sig = None
        with self._authority_lock:
            self._validating_authority = True
            try:
                return super().executescript(*args, **kwargs)
            finally:
                self._validating_authority = False

    def cursor(self, factory=None):
        self.assert_authority()
        return super().cursor(factory or OwnedCursor)

    def commit(self):
        self._validated_data_sig = None
        try:
            self.assert_authority(full=True)
        except BaseException:
            super().rollback()
            raise
        return super().commit()

    def __enter__(self):
        self.assert_authority()
        return super().__enter__()

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self._validated_data_sig = None
            try:
                self.assert_authority(full=True)
            except BaseException:
                super().rollback()
                raise
        return super().__exit__(exc_type, exc, traceback)


class OwnedCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        self.connection.assert_authority()
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self.connection.assert_authority()
        return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self.connection.assert_authority(full=True)
        return super().executescript(*args, **kwargs)


def open_database(path, *, schema, required, initialize, residue=(), validate=None,
                  initialize_missing=True, validate_current=None, timeout=10.0):
    """Initialize only a demonstrably new store; preserve every damaged file."""
    path = filesystem_path(path)
    marker = path.with_name(path.name + '.owner')
    leftovers = (*residue, *(path.with_name(path.name + suffix) for suffix in ('-wal', '-shm', '-journal')))
    if (not initialize_missing and not _present(path) and not _present(marker)
            and not any(_present(p) for p in leftovers)):
        return None  # A genuine first read does not create even a lock file.
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = StartLock(path.with_name(path.name + '.init.lock'))
    lock.acquire(wait=timeout)
    db = None
    try:
        expected = schema + '\n'
        if marker.is_symlink() or path.is_symlink():
            raise ValueError('authoritative state and ownership marker must be regular local files')
        if marker.exists():
            if not marker.is_file():
                raise ValueError('authoritative state ownership marker is not a regular file')
            with marker.open('rb') as source:
                if source.read(len(expected.encode()) + 1) != expected.encode():
                    raise ValueError('authoritative state ownership marker is invalid')
        fresh = not path.exists()
        if fresh and (marker.exists() or any(p.exists() or p.is_symlink() for p in leftovers)):
            raise ValueError('authoritative state database is missing; existing material and receipts were preserved')
        if fresh and not initialize_missing:
            return None  # A read of demonstrably never-initialized authority.
        if not fresh and (not path.is_file() or path.stat().st_size == 0):
            raise ValueError('authoritative state database is empty or invalid; state was not rebuilt')
        before = None if fresh else _file_identity(path)
        db = sqlite3.connect(path if fresh else sqlite_file_uri(path) + '?mode=rw',
                             uri=not fresh, check_same_thread=False, factory=OwnedConnection,
                             timeout=5.0 if timeout is None else timeout)
        db._verified_schemas = {}
        if before is not None and _file_identity(path) != before:
            raise ValueError('authoritative state identity changed while opening it')
        db.row_factory = sqlite3.Row
        if fresh:
            # A crash during initialization remains an explicit incomplete
            # store; later calls may not reinterpret it as never initialized.
            marker.write_bytes(expected.encode('utf-8'))
            marker.chmod(0o600)
            initialize(db)
            db.commit()
        contracts, expected_master = _declared_master(initialize)
        cookie = sqlite3.Connection.execute(db, 'PRAGMA schema_version').fetchone()[0]
        if fresh or _master_snapshot(db, contracts) == expected_master:
            for table, columns in required.items():
                present = set(contracts[table][0]) if table in contracts else {
                    row[1] for row in sqlite3.Connection.execute(db, f'PRAGMA table_info({table})')
                }
                if not set(columns) <= present:
                    raise ValueError(f'authoritative state table {table} is missing or incomplete; state was not rebuilt')
            db._verified_schemas[initialize] = cookie
        else:
            require_columns(db, required)
            require_schema(db, initialize)
        if validate is not None:
            validate(db)
        # Existing valid state can acquire its ownership marker without
        # changing its schema, rows, or business effects.
        if not marker.exists():
            marker.write_bytes(expected.encode('utf-8'))
            marker.chmod(0o600)
        db.execute('PRAGMA journal_mode=WAL')
        if os.environ.get('MINDIE_DISABLE_FSYNC') == '1':
            db.execute('PRAGMA synchronous=OFF')
        db.bind_authority(path, marker, expected.encode(), initialize, validate_current)
        return db
    except BaseException:
        if db is not None:
            db.close()
        raise
    finally:
        lock.release()
