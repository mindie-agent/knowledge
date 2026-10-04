"""Synthetic history -> real parser/scanner/store/outbox; no live history/network."""
import json
from pathlib import Path

import pytest
from material_worker_fixture import package_body, command as summary_command

from conftest import make_admission, write_settings
from lane_support import load_parser
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.history_import import HistoryImportError, import_transcript
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_redaction import install_scanner, ScannerUnavailable


def message(text, *, role='user', channel=None):
    return dict(type='response_item', timestamp='2020-01-01T00:00:00Z',
                payload=dict(type='message', role=role, channel=channel,
                             content=[dict(type='input_text', text=text)]))


def append(path, *records):
    with path.open('a', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')


@pytest.fixture(scope='module')
def scanner():
    return install_scanner()


@pytest.fixture
def case(tmp_path, scanner):
    settings = tmp_path / 'community.json'
    write_settings(settings, roots=[tmp_path])
    authority = Admission(make_admission(tmp_path, project_root=tmp_path))
    store = Store(tmp_path / 'store', 'test')
    engine = Engine(store, settings_path=settings, admission=authority,
                    transcript_adapter=load_parser('codex'), capture_mode='public-transcript',
                    redactor_executable=scanner)
    source = tmp_path / 'history.jsonl'
    append(source, dict(type='session_meta', payload=dict(id='old-unactivated', cwd=str(tmp_path))))
    yield engine, source
    store.close()


def contribute(case, **overrides):
    engine, source = case
    args = dict(session_id='manual-A', token=engine.admission.check('manual-A')['token'],
                source=source, source_session='old-unactivated', source_scope=str(source.parent),
                identity=engine.transcript.identify(source), namespace='codex')
    args.update(overrides)
    return import_transcript(engine, **args)


def test_old_messages_are_explicitly_imported_redacted_and_exportable(case):
    import sys
    engine, source = case
    engine.summary_command = summary_command(title='Synthetic history')
    secret = 'ghp_' + 'AbCdEf0123456789' * 3
    append(source, message('Historical public result ' + secret),
           message('private reasoning canary', role='assistant', channel='analysis'),
           message('public conclusion', role='assistant', channel='final'),
           message('# AGENTS.md instructions\ninjected canary'),
           dict(type='response_item', payload=dict(type='function_call_output', output='tool canary')))
    raw = source.read_bytes()
    result = contribute(case)
    assert result['status'] == 'imported' and result['public_records'] == 2
    assert result['summary']['status'] == 'pending'
    assert engine.admission.active_lease('old-unactivated') is None
    assert source.read_bytes() == raw
    docs = engine.store.drafts_changed(generation=engine._settings().generation)
    assert len(docs) == 1
    body = docs[0]['content']
    assert 'Historical public result' in body and 'public conclusion' in body
    assert all(s not in body for s in (secret, 'private reasoning', 'injected canary', 'tool canary'))
    from mindie_knowledge.loop.export import build_batch
    from mindie_knowledge.loop.documents import parse_entry
    assert build_batch(engine.store, settings=engine._settings()) is None
    summary_due(engine)
    assert contribute(case)['summary']['status'] == 'complete'
    batch_id = build_batch(engine.store, settings=engine._settings())[0]
    from mindie_knowledge.materials.publication import load_batch_payload
    from mindie_knowledge.materials.store import validate_package_files
    payload = load_batch_payload(engine.store, engine.store.batch(batch_id))
    package = validate_package_files({item['path'].split('/', 2)[2]: item['content'] for item in payload['files']})
    assert package_body(package) == body
    assert engine.store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0] == 0


def test_duplicate_and_explicit_extension_keep_one_latest_entry(case):
    engine, source = case
    append(source, message('first historical observation'))
    first = contribute(case)
    assert contribute(case)['status'] == 'unchanged'
    # An appended file is not watched; it changes only on another explicit call.
    append(source, message('second historical observation'))
    assert 'second historical' not in engine.store.drafts_changed()[0]['content']
    second = contribute(case)
    assert second['status'] == 'extended' and second['ref'] != first['ref']
    docs = engine.store.drafts_changed()
    assert len(docs) == 1 and docs[0]['content'].count('first historical observation') == 1
    assert 'second historical observation' in docs[0]['content']
    assert engine.store.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0] == 1
    assert engine.store.db.execute('SELECT COUNT(*) FROM material_streams').fetchone()[0] == 1


@pytest.mark.parametrize('denial', ['inactive', 'sharing-off', 'source-scope', 'wrong-token'])
def test_denial_happens_before_public_read(case, monkeypatch, denial):
    engine, source = case
    args = {}
    if denial == 'inactive':
        args['session_id'] = 'not-active'
    elif denial == 'sharing-off':
        write_settings(engine.settings_path, enabled=False, roots=[source.parent])
    elif denial == 'source-scope':
        args['source_scope'] = str(source.parent / '..' / 'another-project')
    else:
        args['token'] = 'wrong'
    monkeypatch.setattr(engine.transcript, 'read_material', lambda *a, **k: pytest.fail('read before authority'))
    with pytest.raises((ValueError, RuntimeError)):
        contribute(case, **args)
    assert engine.store.drafts_changed() == []


def test_snapshot_excludes_new_appends_and_parser_pages_are_complete(case, monkeypatch):
    engine, source = case
    append(source, *(message('old-record-' + str(i)) for i in range(205)))
    expected = engine.transcript.identify(source)
    append(source, message('appended-after-selection'))
    result = contribute(case, identity=expected)
    assert result['public_records'] == 205
    body = engine.store.drafts_changed()[0]['content']
    assert 'old-record-204' in body and 'appended-after-selection' not in body
    assert contribute(case)['status'] == 'extended'


def test_multiline_secret_is_redacted_across_parser_pages(case):
    engine, source = case
    append(source, *(message('padding-' + str(i)) for i in range(199)))
    append(source, message('-----BEGIN PRIVATE KEY-----'), message('LONG_KEY_END_CANARY'))
    contribute(case)
    assert 'LONG_KEY_END_CANARY' not in engine.store.drafts_changed()[0]['content']


@pytest.mark.parametrize('ending', ['', '-----END EC PRIVATE KEY-----', '-----END RSA PRIVATE KEY-----'])
def test_private_key_has_no_length_or_blank_line_escape(scanner, ending):
    from mindie_knowledge.loop.transcript_redaction import redact
    raw = ('safe prefix\n-----BEGIN RSA PRIVATE KEY-----\n' + 'A' * 40000
           + '\n\nLONG_PRIVATE_CANARY\n' + ending + '\npublic suffix')
    masked, _ = redact(raw, executable=scanner, key=b'k' * 32)
    assert 'LONG_PRIVATE_CANARY' not in masked
    assert ('public suffix' in masked) == (ending == '-----END RSA PRIVATE KEY-----')
    assert redact(masked, executable=scanner, key=b'k' * 32)[0] == masked


@pytest.mark.parametrize('damage', ['tail', 'wrong-owner', 'replaced'])
def test_incomplete_or_replaced_source_never_commits_a_partial_entry(case, damage):
    engine, source = case
    append(source, message('first valid text'))
    expected = engine.transcript.identify(source)
    args = {}
    if damage == 'tail':
        with source.open('ab') as stream:
            stream.write(b'{"incomplete":')
    elif damage == 'wrong-owner':
        args['source_session'] = 'different'
    else:
        source.unlink()
        append(source, dict(type='session_meta', payload=dict(id='replacement')), message('foreign'))
        args['identity'] = expected
    with pytest.raises(HistoryImportError):
        contribute(case, **args)
    if damage == 'tail':
        row = engine.store.db.execute('SELECT entry_id FROM entries').fetchone()
        assert 'first valid text' in engine.store.get(engine.store.ref(row[0]))['content']
        assert engine.store.quarantined_entries()[row[0]] == 'history-intake'
        from mindie_knowledge.loop.export import build_batch
        assert build_batch(engine.store, settings=engine._settings()) is None
    else:
        assert engine.store.drafts_changed() == []


def test_revocation_mid_read_stops_before_redaction_and_commit(case, monkeypatch):
    engine, source = case
    append(source, message('public before revocation'))
    real = engine.transcript.read_material
    def revoke(*args, **kwargs):
        result = real(*args, **kwargs)
        write_settings(engine.settings_path, enabled=False, roots=[source.parent])
        return result
    monkeypatch.setattr(engine.transcript, 'read_material', revoke)
    with pytest.raises(RuntimeError):
        contribute(case)
    assert engine.store.drafts_changed() == []


def test_scanner_failure_does_not_create_an_import_receipt(case, monkeypatch):
    engine, source = case
    append(source, message('public text'))
    engine.redactor_executable = str(source.parent / 'absent-scanner')
    with pytest.raises(ScannerUnavailable):
        contribute(case)
    assert engine.store.db.execute('SELECT COUNT(*) FROM material_streams').fetchone()[0] == 0
    assert engine.store.drafts_changed() == []


def test_changed_prefix_is_reported_without_overwriting_latest_body(case):
    engine, source = case
    append(source, message('original history'))
    contribute(case)
    before = engine.store.drafts_changed()[0]
    source.write_text(source.read_text().replace('original history', 'modified history'))
    with pytest.raises(HistoryImportError, match='changed'):
        contribute(case)
    assert engine.store.drafts_changed()[0] == before


def test_repeat_after_header_change_returns_current_reference(case):
    from mindie_knowledge.loop.store import digest
    engine, source = case
    append(source, message('historical body'))
    first = contribute(case)
    doc = engine.store.drafts_changed()[0]
    engine.store.update_draft_header(doc['entry_id'], expected_body=digest(doc['content']),
                                     title='Better retrieval title', summary='Updated retrieval summary',
                                     generation=engine._settings().generation)
    result = contribute(case)
    assert result['status'] == 'unchanged' and result['ref'] != first['ref']
    assert engine.store.explain(result['ref'])['content'] == doc['content']


def test_receipt_rolls_back_with_failed_body_commit(case, monkeypatch):
    engine, source = case
    append(source, message('atomic import'))
    real = engine.store._bind_material_draft
    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise OSError('simulated commit interruption')
    monkeypatch.setattr(engine.store, '_bind_material_draft', fail)
    with pytest.raises(OSError):
        contribute(case)
    assert engine.store.drafts_changed() == []
    assert engine.store.db.execute('SELECT COUNT(*) FROM material_streams').fetchone()[0] == 0


def test_ordinary_capture_does_not_import_pre_activation_history(case):
    engine, source = case
    source.write_text(source.read_text().replace('old-unactivated', 'manual-A'))
    append(source, message('old history is not authorized by an ordinary Stop'))
    capture = engine.capture(session_id='manual-A', turn_id='ordinary', transcript_path=str(source))
    engine._process(capture['id'])
    assert engine.store.drafts_changed() == []
    assert engine.store.db.execute('SELECT COUNT(*) FROM material_streams').fetchone()[0] == 0


def summary_due(engine):
    import time
    from mindie_knowledge.loop.transcript_capture import summarize_due
    engine.last_activity = time.monotonic() - 10
    with engine.store._write_txn():
        engine.store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    summarize_due(engine)


def test_import_summary_uses_saved_public_body_once_without_reopening_history(case):
    import sys
    engine, source = case
    marker = source.parent / 'calls'
    engine.summary_command = summary_command(title='Useful title', calls=marker, required='technical result')
    append(source, message('technical result'))
    contribute(case)
    before = engine.store.drafts_changed()[0]['content']
    assert contribute(case)['status'] == 'unchanged'
    source.unlink()  # Background summary reads the saved redacted body only.
    summary_due(engine)
    summary_due(engine)
    after = engine.store.drafts_changed()[0]
    assert after['content'] == before and after['title'] == 'Useful title'
    assert marker.read_text() == 'x'
    task = dict(engine.store.db.execute('SELECT * FROM transcript_tasks').fetchone())
    assert task['summary_status'] == 'complete'
    assert json.loads(task['authorization'])['session'] == 'manual-A'
    assert engine.admission.active_lease('old-unactivated') is None
    assert engine.store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0] == 0
    assert engine.store.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0] == 1


def test_import_summary_does_not_spawn_after_contribution_revocation(case):
    import sys
    engine, source = case
    marker = source.parent / 'must-not-run'
    engine.summary_command = [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()']
    append(source, message('saved result'))
    contribute(case)
    write_settings(engine.settings_path, enabled=False, roots=[source.parent])
    summary_due(engine)
    assert not marker.exists()
    assert engine.store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'cancelled'
    assert 'saved result' in engine.store.drafts_changed()[0]['content']


def test_missing_history_job_is_reported_and_not_exported(case):
    from mindie_knowledge.loop.export import build_batch
    engine, source = case
    append(source, message('saved historical result'))
    first = contribute(case)
    assert first['summary']['status'] == 'pending'
    with engine.store._write_txn():
        engine.store.db.execute('DELETE FROM transcript_tasks')
    result = contribute(case)
    assert result['status'] == 'unchanged'
    assert result['summary']['status'] == 'missing'
    assert not engine.store.has_changed_drafts(generation=engine._settings().generation, ready_only=True)
    assert build_batch(engine.store, settings=engine._settings()) is None


def test_missing_material_batch_is_visible_without_recreating_model_work(case):
    from mindie_knowledge.loop.export import build_batch
    engine, source = case
    marker = source.parent / 'must-not-call-model'
    engine.summary_command = summary_command(calls=marker)
    append(source, message('Material survives its missing queue record.'))
    assert contribute(case)['summary']['status'] == 'pending'
    before = engine.store.drafts_changed()[0]['content']
    with engine.store._write_txn():
        engine.store.db.execute('DELETE FROM material_batches')
    summary_due(engine)
    result = contribute(case)
    assert result['status'] == 'unchanged'
    assert result['summary']['status'] == 'missing'
    assert 'batch job is missing' in result['summary']['detail']
    assert engine.status()['transcript_summaries'] == {'missing': 1}
    assert engine.store.drafts_changed()[0]['content'] == before
    assert engine.store.db.execute('SELECT COUNT(*) FROM material_batches').fetchone()[0] == 0
    assert engine.store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0] == 'pending'
    assert not marker.exists()
    assert build_batch(engine.store, settings=engine._settings()) is None


def test_k3_interrupted_import_retains_pages_but_requires_complete_selected_snapshot(case):
    engine, source = case
    engine.summary_command = summary_command()
    append(source, message('K3-04 resumed precision observation remains unresolved.'))
    with source.open('ab') as stream:
        stream.write(b'{"partial":')
    with pytest.raises(HistoryImportError):
        contribute(case)
    summary_due(engine)
    from mindie_knowledge.loop.export import build_batch
    assert build_batch(engine.store, settings=engine._settings()) is None
    assert set(engine.store.quarantined_entries().values()) == {'history-intake'}
    with source.open('ab') as stream:
        stream.write(b'null}\n')
    result = contribute(case)
    assert result['consumed_bytes'] == source.stat().st_size
    assert engine.store.quarantined_entries() == {}
    assert build_batch(engine.store, settings=engine._settings()) is not None


def test_k3_explicit_same_body_retry_preserves_failure_call_cost(case):
    engine, source = case
    engine.summary_command = summary_command(fail=True)
    append(source, message('K3-02 checkpoint correction invalidates the early benchmark interpretation.'))
    contribute(case)
    summary_due(engine)
    assert contribute(case)['summary']['status'] == 'failed'
    before = engine.status()['summary_usage']
    engine.summary_command = summary_command()
    summary_due(engine)
    assert engine.status()['summary_usage'] == before
    assert contribute(case, retry_summary=True)['summary']['status'] == 'pending'
    summary_due(engine)
    assert contribute(case)['summary']['status'] == 'complete'
    assert engine.status()['summary_usage']['model_calls'] == 2
