"""Bounded delivery projection over existing full diagnostic records.

Vendored unchanged into the adapter bootstrap so a broken runtime can still
leave an Agent-visible incident. No task text, stderr or exception messages.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

LABEL = re.compile(r'[A-Za-z][A-Za-z0-9_.:-]{0,119}\Z')
HEX = re.compile(r'[0-9a-f]{32}\Z')
SLOTS = 64


def root():
    override = os.environ.get('MINDIE_DIAGNOSTICS_ROOT')
    if override:
        return Path(override).expanduser().absolute()
    if os.name == 'nt' and not os.environ.get('XDG_STATE_HOME') and os.environ.get('LOCALAPPDATA'):
        return Path(os.environ['LOCALAPPDATA']) / 'mindie/diagnostics'
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'mindie/diagnostics'


def _present(path):
    # Keep this bootstrap module standalone: only a real missing path beneath
    # an accessible directory is optional first use, never an I/O failure.
    try:
        path.lstat()
    except FileNotFoundError as missing:
        for parent in path.parents:
            try:
                mode = parent.stat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(parent)) from None
            break
        else:
            raise missing
        return False
    return True


def _open(create=False):
    path = root() / 'agent-delivery.sqlite3'
    marker = path.with_name(path.name + '.owner')
    expected = 'mindie-agent-delivery/1\n'
    existed = _present(path)
    if marker.is_symlink() or (_present(marker) and (not marker.is_file() or marker.stat().st_size != len(expected)
                                                   or marker.read_text(encoding='utf-8') != expected)):
        raise ValueError('diagnostic projection ownership marker is invalid')
    if not existed and _present(marker):
        raise ValueError('diagnostic delivery state is missing; pending incidents were not rebuilt')
    if not create and not existed:
        return None
    if path.is_symlink() or (_present(path) and not path.is_file()):
        raise ValueError('diagnostic projection must be a regular local file')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=0.1)
    try:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in db.execute('PRAGMA table_info(pending)')}
        if not columns:
            if existed or _present(marker):
                raise ValueError('diagnostic delivery schema is missing; pending incidents were not rebuilt')
            marker.write_bytes(expected.encode('utf-8'))
            marker.chmod(0o600)
            db.execute('CREATE TABLE pending (key TEXT PRIMARY KEY, generation INTEGER NOT NULL, delivered INTEGER NOT NULL, value TEXT NOT NULL)')
        expected_columns = {
            'key': ('key', 'TEXT', 0, None, 1),
            'generation': ('generation', 'INTEGER', 1, None, 0),
            'delivered': ('delivered', 'INTEGER', 1, None, 0),
            'value': ('value', 'TEXT', 1, None, 0),
        }
        actual_columns = {row[1]: tuple(row[1:]) for row in db.execute('PRAGMA table_info(pending)')}
        if any(actual_columns.get(name) != value for name, value in expected_columns.items()):
            raise ValueError('diagnostic delivery schema or constraints are incomplete')
        if not _present(marker):
            marker.write_bytes(expected.encode('utf-8'))
            marker.chmod(0o600)
        db.commit()
        path.chmod(0o600)
        return db
    except BaseException:
        db.close()
        raise


def enqueue(component, operation, stage, code, diagnostic):
    """Keep full evidence in its original store; coalesce only its projection."""
    if not all(isinstance(x, str) and LABEL.fullmatch(x) for x in (component, operation, stage, code)):
        raise ValueError('invalid diagnostic projection labels')
    value = dict(component=component, operation=operation, stage=stage, code=code,
                 logging_failed=diagnostic.get('logging_failed') is True)
    incident = diagnostic.get('incident_id')
    if isinstance(incident, str) and HEX.fullmatch(incident):
        value['incident_id'] = incident
    value['record_ref'] = dict(directory=str(root() / 'events' / component),
                               incident_id=value.get('incident_id'))
    key = hashlib.sha256(json.dumps([component, operation, stage, code]).encode()).hexdigest()
    db = _open(True)
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT generation FROM pending WHERE key=?', (key,)).fetchone()
        if row is None and db.execute("SELECT count(*) FROM pending WHERE key!='overflow'").fetchone()[0] >= SLOTS:
            key = 'overflow'
            value = dict(code='projection_overflow', record_ref=dict(directory=str(root() / 'events')))
        db.execute('INSERT INTO pending VALUES(?,1,0,?) ON CONFLICT(key) DO UPDATE SET generation=generation+1,value=excluded.value',
                   (key, json.dumps(value, separators=(',', ':'))))
        db.commit()
    finally:
        db.close()


def pending(limit=8):
    db = _open()
    if db is None:
        return None
    try:
        rows = db.execute('SELECT * FROM pending WHERE generation>delivered ORDER BY key LIMIT ?', (limit,)).fetchall()
        total = db.execute('SELECT coalesce(sum(generation-delivered),0) FROM pending').fetchone()[0]
        if not rows:
            return None
        items = [dict(json.loads(row['value']), delivery_key=row['key'],
                      delivery_generation=row['generation'], count=row['generation']-row['delivered']) for row in rows]
        return dict(items=items, remaining=total-sum(item['count'] for item in items))
    finally:
        db.close()


def acknowledge(projection):
    """Called only after the containing protocol response was written."""
    if not isinstance(projection, dict):
        return
    db = _open()
    if db is None:
        return
    try:
        with db:
            for item in projection.get('items', []):
                if (isinstance(item, dict) and isinstance(item.get('delivery_key'), str)
                        and type(item.get('delivery_generation')) is int):
                    db.execute('UPDATE pending SET delivered=max(delivered,min(generation,?)) WHERE key=?',
                               (item['delivery_generation'], item['delivery_key']))
    finally:
        db.close()
