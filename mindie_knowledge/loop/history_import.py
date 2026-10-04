"""Explicit source selection using the same incremental material pipeline as Stop.

No source discovery, private transcript copies or separate history organizer.
Each page commits its redacted blocks, cursor and scanner state together. A
repeat validates the consumed source prefix and resumes only its new suffix.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .activation import activation_epoch
from .store import digest, session_key
from ..materials.ingest import prepare_increment


class HistoryImportError(ValueError):
    """A static content-free diagnostic safe to return to the operator."""


def _hash_prefix(source, end):
    result = hashlib.sha256()
    with open(source, 'rb') as stream:
        remaining = end
        while remaining:
            value = stream.read(min(1024 * 1024, remaining))
            if not value:
                raise HistoryImportError('source was truncated before the imported boundary')
            result.update(value)
            remaining -= len(value)
    return result


def import_transcript(engine, *, session_id, token, source, source_session,
                      source_scope, identity, namespace, retry_summary=False):
    if engine.capture_mode != 'public-transcript' or engine.admission is None:
        raise HistoryImportError('history import requires public-transcript mode and admission')
    lease = engine.admission.check(session_id, token)
    settings = engine._settings()
    scope = engine.admission.scope_root(session_id)
    authorization = dict(session=session_id, generation=settings.generation, scope=scope,
                         activation_epoch=activation_epoch(lease['token']))

    def gate():
        current = engine._revalidate(authorization)
        if not scope or not current.in_scope(scope) or not current.in_scope(source_scope):
            raise HistoryImportError('source or current task is outside the contribution scope')

    gate()
    source = str(Path(source).resolve())
    if identity is None or identity.path != source:
        raise HistoryImportError('source identity is unavailable')
    parser, store = engine.transcript, engine.store
    if parser is None:
        raise HistoryImportError('transcript parser is unavailable')
    key = digest(['selected-history/2', namespace, source_session, settings.generation])
    entry_id = digest(['task-experience/1', store.domain, key])
    owner = store.opaque_for(session_key('selected-history:' + key))
    prior = store.material_stream(key)
    was_existing = prior is not None
    cursor = prior['source_cursor'] if prior else 0
    if cursor > identity.size:
        raise HistoryImportError('previously imported source was truncated')
    prefix = _hash_prefix(source, cursor)
    if prior:
        saved_identity = json.loads(prior['source_identity'])
        if saved_identity['prefix_sha256'] != prefix.hexdigest():
            raise HistoryImportError('previously imported source changed; no overwrite performed')
    # A selected snapshot is publishable only after every page is admitted.
    # This durable intake marker survives a crash between committed pages;
    # a later explicit resume clears only its own marker on complete EOF.
    if cursor < identity.size:
        with store._write_txn():
            import time
            store.db.execute('INSERT OR IGNORE INTO entry_quarantine VALUES(?,?,?,?)',
                             (entry_id, 'history-intake', 'selected history snapshot is incomplete', time.time()))
    records = skipped = pages = added = 0
    while cursor < identity.size:
        gate()
        inc = parser.read_material(source, cursor, session_id=source_session, not_before=None,
                                   expected=identity, scan_until=identity.size)
        if inc['status'] not in {'ok', 'unchanged'} or inc.get('coverage') or inc.get('discarded_records'):
            raise HistoryImportError('source could not be completely parsed; committed pages remain resumable')
        if inc['end'] <= cursor or inc['end'] > identity.size:
            raise HistoryImportError('source has an incomplete record or changed snapshot')
        # Read only this newly admitted raw range to extend the private digest;
        # it is never persisted or sent to a model.
        with open(source, 'rb') as stream:
            stream.seek(cursor)
            remaining = inc['end'] - cursor
            while remaining:
                raw = stream.read(min(1024 * 1024, remaining))
                if not raw:
                    raise HistoryImportError('source changed during import')
                prefix.update(raw)
                remaining -= len(raw)
        scanner_state = json.loads(prior['redaction_state']) if prior else {}
        prepared = prepare_increment(task_id=entry_id, text=inc['text'], start=cursor,
                                     end=inc['end'], source_digest=inc['digest'],
                                     scanner_state=scanner_state, executable=engine.redactor_executable,
                                     key=store.redaction_key(), private_paths=(source_scope, scope, str(Path.home())))
        gate()
        if prepared['blocks']:
            store.commit_material_increment(stream_key=key, entry_id=entry_id, prepared=prepared,
                                            start=cursor, end=inc['end'], source_identity=json.dumps(dict(
                                                native=identity.serialize(), prefix_sha256=prefix.hexdigest())),
                                            authorization=authorization, owner=owner, observed_stream=prior)
            added += len(prepared['blocks'])
        else:
            with store._write_txn():
                if store.material_stream(key) != prior:
                    raise HistoryImportError('source was imported concurrently')
                import time
                store.db.execute('INSERT OR REPLACE INTO material_streams VALUES(?,?,?,?,?,?,?,?)',
                                 (key, entry_id, inc['end'], json.dumps(dict(native=identity.serialize(),
                                  prefix_sha256=prefix.hexdigest())), json.dumps(scanner_state), settings.generation,
                                  json.dumps(authorization), time.time()))
        cursor = inc['end']
        records += inc['records']
        skipped += inc['skipped_records']
        pages += 1
        prior = store.material_stream(key)
    gate()
    with store._write_txn():
        store.db.execute("DELETE FROM entry_quarantine WHERE entry_id=? AND kind='history-intake'", (entry_id,))
    row = store._row(entry_id)
    if retry_summary and row:
        gate()
        with store._write_txn():
            import time
            store.db.execute("UPDATE material_batches SET status='retry-requested',detail='' "
                             "WHERE entry_id=? AND status IN ('failed','outcome_unknown')", (entry_id,))
            store.db.execute("UPDATE transcript_tasks SET summary_status='pending',summary_detail='',summary_due=? "
                             "WHERE entry_id=?", (time.time(), entry_id))
    task = store.transcript_task(key)
    return dict(status=(('extended' if was_existing else 'imported') if added else 'unchanged') if row else 'empty',
                ref=store.ref(entry_id, row['draft_revision'] or row['published_revision']) if row else None,
                summary=(dict(status=task['summary_status'], detail=task['summary_detail']) if task else
                         dict(status='missing', detail='material index job is missing') if row else None),
                publication='pending' if row else None, snapshot_bytes=identity.size,
                consumed_bytes=cursor, public_records=records, skipped_records=skipped,
                discarded_records=0, pages=pages, new_blocks=added)
