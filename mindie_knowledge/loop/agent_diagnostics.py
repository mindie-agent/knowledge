"""Bounded delivery projection over existing full diagnostic records.

Vendored unchanged into the adapter bootstrap so a broken runtime can still
leave an Agent-visible incident. No task text, stderr or exception messages.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

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


def _open(create=False):
    path = root() / 'agent-delivery.sqlite3'
    if not create and not path.exists():
        return None
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('diagnostic projection must be a regular local file')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=0.1)
    try:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS pending (key TEXT PRIMARY KEY, generation INTEGER NOT NULL, delivered INTEGER NOT NULL, value TEXT NOT NULL)')
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
