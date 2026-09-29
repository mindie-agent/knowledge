"""Explicit historical contribution, called only by an adapter's manual entry.

No discovery, scheduler, raw staging copies or new publication protocol. Each
named transcript is read to a fixed snapshot, redacted, and committed once.
Only the latest small import receipt is kept; the ordinary outbox owns delivery.
The adapter obtains source identity/scope from its native metadata after checking
the current session's admission. Historical sessions need no retroactive lease.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from .activation import activation_epoch
from .store import digest, new_identity, session_key
from .transcript_capture import fallback_header
from .transcript_redaction import redact


class HistoryImportError(ValueError):
    """A static, content-free reason safe to return to the operator."""


def _fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _receipt(store, key):
    with store.lock:
        row = store.db.execute(
            'SELECT * FROM history_imports WHERE source_key=?', (key,)
        ).fetchone()
    return dict(row) if row else None


def import_transcript(engine, *, session_id, token, source, source_session,
                      source_scope, identity, namespace):
    """Contribute one user-selected snapshot, with no automatic history reads.

    Repeated imports are no-ops. A later explicit import of an appended source
    adds only its new public suffix to the same entry. Changed earlier material
    is reported, never silently used to overwrite a published correction.
    Import receipts, draft writes and publication grants commit atomically.
    """
    if engine.capture_mode != 'public-transcript' or engine.admission is None:
        raise HistoryImportError('history import requires public-transcript mode and admission')
    lease = engine.admission.check(session_id, token)
    settings = engine._settings()
    scope = engine.admission.scope_root(session_id)
    row = dict(session=session_id, generation=settings.generation, scope=scope,
               activation_epoch=activation_epoch(lease['token']))

    def gate():
        current = engine._revalidate(row)
        if not scope or not current.in_scope(scope) or not current.in_scope(source_scope):
            raise HistoryImportError('source or current task is outside the contribution scope')
        return current

    gate()
    source = str(Path(source).resolve())
    if identity is None or identity.path != source:
        raise HistoryImportError('source identity is unavailable')
    parser, store = engine.transcript, engine.store
    if parser is None:
        raise HistoryImportError('transcript parser is unavailable')
    # One source at a time. Page targets never truncate a public message; the
    # canonical entry's existing platform envelope still applies at commit.
    # Redact the complete public projection so multiline secrets spanning parser
    # pages cannot escape through independent per-page scans.
    text = io.StringIO()
    cursor = records = skipped = discarded = 0
    while cursor < identity.size:
        gate()
        inc = parser.read_material(source, cursor, session_id=source_session,
                                   not_before=None, expected=identity,
                                   scan_until=identity.size)
        if inc['status'] not in {'ok', 'unchanged'} or inc.get('coverage'):
            raise HistoryImportError('source could not be completely parsed')
        if inc['end'] <= cursor or inc['end'] > identity.size:
            raise HistoryImportError('source has an incomplete record or changed snapshot')
        if inc['text']:
            if text.tell():
                text.write('\n\n')
            text.write(inc['text'])
        cursor = inc['end']
        records += inc['records']
        skipped += inc['skipped_records']
        discarded += len(inc.get('discarded_records', []))
    gate()
    body = text.getvalue().strip()
    text.close()
    coverage = dict(snapshot_bytes=identity.size, public_records=records,
                    skipped_records=skipped, discarded_records=discarded)
    if not body:
        return dict(status='empty', **coverage)
    body, rules = redact(body, executable=engine.redactor_executable,
                         key=store.redaction_key(),
                         private_paths=(source_scope, scope, str(Path.home())))
    body = body.strip()
    gate()
    key = digest(['history-import/1', namespace, source_session])
    prior = _receipt(store, key)
    fingerprint = _fingerprint(body)
    if prior and prior['content_digest'] == fingerprint:
        # Publication/metadata updates may have retired the import revision.
        # Return the current readable reference, not a pruned draft reference.
        with store.lock:
            current = store._row(prior['entry_id'])
            revision = (current['draft_revision'] or current['published_revision']) if current else None
        return dict(status='unchanged',
                    ref=store.ref(prior['entry_id'], revision) if revision else None,
                    **coverage)
    if prior:
        if prior['generation'] != settings.generation:
            raise HistoryImportError('existing import belongs to another contribution scope')
        if _fingerprint(body[:prior['content_chars']]) != prior['content_digest']:
            raise HistoryImportError('previously imported public content changed; no overwrite performed')
        addition = body[prior['content_chars']:].strip()
        entry_id = prior['entry_id']
        existing = store._row(entry_id)
        if existing is None:
            raise HistoryImportError('previous import entry is unavailable')
        if not existing['draft_revision']:
            gate()
            if not engine._restore_sent_draft(entry_id, settings.generation):
                raise HistoryImportError('previously published entry cannot be extended')
    else:
        addition, entry_id = body, new_identity()
    title, summary = fallback_header(addition)
    with store._write_txn():
        gate()
        if _receipt(store, key) != prior:
            raise HistoryImportError('another explicit import changed this source; inspect its result')
        owner = store.opaque_for(session_key('history:' + key))
        if prior:
            doc, _ = store.append_observation(
                entry_id, addition, marker=digest(['history', key, fingerprint]),
                producer=owner, generation=settings.generation,
                header=dict(title=title, summary=summary),
            )
        else:
            doc = store.create_draft(kind='experience', title=title, summary=summary,
                                     content=body, entry_id=entry_id, owner=owner,
                                     generation=settings.generation)
        store.db.execute('INSERT OR REPLACE INTO history_imports VALUES(?,?,?,?,?,?)',
                         (key, entry_id, len(body), fingerprint, settings.generation, doc['revision']))
    return dict(status='imported' if prior is None else 'extended',
                ref=store.ref(entry_id, doc['revision']), redaction_rules=rules,
                publication='pending', **coverage)
