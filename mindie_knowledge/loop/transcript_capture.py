"""Public-transcript path. Body commit is independent of all models.

The caller supplies the harness parser and existing authority/cursor machinery.
Body, cursor and continuation commit together; a process crash before commit
leaves the same input due. Summary calls are coalesced, once per body version,
and their only write capability is the title/summary store method.
"""
import json
import time
from pathlib import Path

from .process import bounded_run
from .store import canonical, digest, new_identity
from .transcript_redaction import ScannerUnavailable, redact

MODE = "public-transcript"
SUMMARY_SECONDS = 45
SUMMARY_SETTLE_SECONDS = 3


def excerpt(text, size):
    return text.encode('utf-8')[:size].decode('utf-8', 'ignore').strip()


def fallback_header(text):
    # Clearly an excerpt, not a claim that a semantic summary succeeded.
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('### ')]
    first = lines[0] if lines else 'Public conversation'
    return excerpt(first, 240)[:120], 'Conversation excerpt: ' + excerpt('\n'.join(lines), 1500)


def capture(engine, row, text, region):
    from .engine import CursorConflict
    store = engine.store
    inc = region.get('inc') or {}
    if not inc or inc.get('coverage'):
        raise ValueError('public transcript is incomplete; body not saved as complete')
    masked, rules = redact(text, executable=engine.redactor_executable, key=store.redaction_key(),
                           private_paths=(row['scope'], str(Path.home())))
    engine._revalidate(row)
    owner = store.opaque_for(row['root_session'])
    task_key = digest([MODE, store.domain, row['session'], row['generation']])
    task = store.transcript_task(task_key)
    entry_id = task['entry_id'] if task else new_identity()
    existing = store._row(entry_id)
    # Compaction keeps the receipt, not a second full body. Restore through
    # the existing authoritative PR/main path before extending it.
    if existing is not None and not existing['draft_revision']:
        if not engine._restore_sent_draft(entry_id, row['generation']):
            raise ValueError('transcript entry cannot be restored for append')
    with store._write_txn():
        engine._revalidate(row)
        reserved = region['reserve'](inc['start'], inc['end'], inc['digest'], status='succeeded')
        if reserved is None:
            raise CursorConflict('cursor changed before local body commit')
        if task:
            title, summary = fallback_header(masked)
            doc, _ = store.append_observation(entry_id, masked, marker=inc['digest'], producer=owner,
                                              generation=row['generation'], header=dict(title=title, summary='Latest conversation excerpt: ' + summary.removeprefix('Conversation excerpt: ')))
        else:
            title, summary = fallback_header(masked)
            doc = store.create_draft(kind='experience', title=title, summary=summary, content=masked,
                                     owner=owner, entry_id=entry_id, generation=row['generation'])
        store.finish_region(reserved, 'succeeded', 'public messages saved; model calls=0')
        store.db.execute('INSERT OR REPLACE INTO transcript_tasks VALUES(?,?,?,?,?,?,?,?)',
                         (task_key, entry_id, row['id'], digest(doc['content']),
                          'pending' if engine.summary_command else 'excerpt', '', time.time(), time.time() + SUMMARY_SETTLE_SECONDS))
        detail = canonical(dict(pipeline=MODE, refs=[store.ref(entry_id, doc['revision'])],
                                redaction_rules=rules, body_model_calls=0,
                                discarded_records=inc.get('discarded_records', [])))
        store.mark_capture(row['id'], 'organized', detail)
        if inc.get('more'):
            store.defer_capture(row['id'], due=time.time(), reason='more public transcript bytes')
    engine.last_activity = time.monotonic()


def summarize_due(engine):
    from .engine import AdmissionUnreadable, GateFault
    from .process import MaintenanceCancelled
    if not engine.summary_command or time.monotonic() - engine.last_activity < SUMMARY_SETTLE_SECONDS:
        return
    store = engine.store
    with store.lock:
        found = store.db.execute("SELECT * FROM transcript_tasks WHERE summary_status='pending' AND summary_due<=? ORDER BY updated LIMIT 1", (time.time(),)).fetchone()
    if found is None or not engine.begin_work():
        return
    task = dict(found)
    reserved = False
    status, detail = 'failed', ''
    try:
        row = store.capture_row(task['capture_id'])
        engine._summary_cancel.clear()
        engine._gate_live()
        engine._revalidate(row)
        with store._write_txn():
            current = store._row(task['entry_id'])
            doc = store._revision_doc(task['entry_id'], current['draft_revision']) if current and current['draft_revision'] else None
            if doc is None or digest(doc['content']) != task['body_digest']:
                status = 'superseded'
                return
            changed = store.db.execute("UPDATE transcript_tasks SET summary_status='running' WHERE task_key=? AND body_digest=? AND summary_status='pending'", (task['task_key'], task['body_digest'])).rowcount
            if not changed:
                return
            reserved = True
        body, _ = redact(doc['content'], executable=engine.redactor_executable, key=store.redaction_key(),
                         private_paths=(row['scope'], str(Path.home())))
        payload = canonical(dict(role='summarize', text=body))
        raw = bounded_run(engine.summary_command, payload, timeout=SUMMARY_SECONDS, max_output=8192, cancel=engine._summary_cancel)
        result = json.loads(raw)
        if not isinstance(result, dict) or set(result) != {'title', 'summary'}:
            raise ValueError('summary must contain only title and summary')
        if not all(isinstance(result[k], str) and result[k].strip() for k in result):
            raise ValueError('summary metadata must be nonempty text')
        clean = {}
        for field, value in result.items():
            clean[field], _ = redact(value.strip(), executable=engine.redactor_executable, key=store.redaction_key(),
                                      private_paths=(row['scope'], str(Path.home())))
        engine._gate_live()
        engine._revalidate(row)
        applied = store.update_draft_header(task['entry_id'], expected_body=task['body_digest'], generation=row['generation'], **clean)
        status = 'complete' if applied else 'superseded'
    except MaintenanceCancelled:
        status = 'pending' if engine.stop.is_set() else 'cancelled'
        detail = 'service stopped; body retained' if status == 'pending' else 'authority revoked'
    except ScannerUnavailable:
        # No model was needed to retry local redaction. Keep this body version
        # due even if it is the final Stop; do not wait for another user turn.
        status, detail = 'pending', 'scanner unavailable; body retained'
    except (AdmissionUnreadable, GateFault):
        if reserved:
            status, detail = 'pending', 'authority unavailable; body retained'
        else:
            with store._write_txn():
                store.db.execute('UPDATE transcript_tasks SET summary_due=? WHERE task_key=? AND body_digest=?',
                                 (time.time() + 30, task['task_key'], task['body_digest']))
    except Exception as exc:
        # Provider errors can carry source text/secrets. Persist only a type.
        detail = type(exc).__name__
    finally:
        if reserved or status in {'superseded', 'cancelled'}:
            with store._write_txn():
                store.db.execute('UPDATE transcript_tasks SET summary_status=?, summary_detail=?, summary_due=? WHERE task_key=? AND body_digest=?',
                                 (status, detail, time.time() + 30, task['task_key'], task['body_digest']))
        engine.end_work()
