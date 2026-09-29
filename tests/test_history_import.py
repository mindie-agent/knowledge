"""Synthetic history -> real parser/scanner/store/outbox; no live history/network."""
import json
from pathlib import Path

import pytest

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
    engine, source = case
    secret = 'ghp_' + 'AbCdEf0123456789' * 3
    append(source, message('Historical public result ' + secret),
           message('private reasoning canary', role='assistant', channel='analysis'),
           message('public conclusion', role='assistant', channel='final'),
           message('# AGENTS.md instructions\ninjected canary'),
           dict(type='response_item', payload=dict(type='function_call_output', output='tool canary')))
    raw = source.read_bytes()
    result = contribute(case)
    assert result['status'] == 'imported' and result['public_records'] == 2
    assert engine.admission.active_lease('old-unactivated') is None
    assert source.read_bytes() == raw
    docs = engine.store.drafts_changed(generation=engine._settings().generation)
    assert len(docs) == 1
    body = docs[0]['content']
    assert 'Historical public result' in body and 'public conclusion' in body
    assert all(s not in body for s in (secret, 'private reasoning', 'injected canary', 'tool canary'))
    from mindie_knowledge.loop.export import build_batch
    from mindie_knowledge.loop.documents import parse_entry
    batch_id = build_batch(engine.store, settings=engine._settings())[0]
    payload = json.loads(engine.store.batch(batch_id)['batch'])
    assert parse_entry(payload['files'][0]['content'].encode())['content'] == body
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
    assert engine.store.db.execute('SELECT COUNT(*) FROM history_imports').fetchone()[0] == 1


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
    assert engine.store.db.execute('SELECT COUNT(*) FROM history_imports').fetchone()[0] == 0
    assert engine.store.drafts_changed() == []


def test_changed_prefix_is_reported_without_overwriting_latest_body(case):
    engine, source = case
    append(source, message('original history'))
    contribute(case)
    before = engine.store.drafts_changed()[0]
    source.write_text(source.read_text().replace('original history', 'edited history'))
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
    real = engine.store.create_draft
    def fail(**kwargs):
        real(**kwargs)
        raise OSError('simulated commit interruption')
    monkeypatch.setattr(engine.store, 'create_draft', fail)
    with pytest.raises(OSError):
        contribute(case)
    assert engine.store.drafts_changed() == []
    assert engine.store.db.execute('SELECT COUNT(*) FROM history_imports').fetchone()[0] == 0


def test_ordinary_capture_does_not_import_pre_activation_history(case):
    engine, source = case
    source.write_text(source.read_text().replace('old-unactivated', 'manual-A'))
    append(source, message('old history is not authorized by an ordinary Stop'))
    capture = engine.capture(session_id='manual-A', turn_id='ordinary', transcript_path=str(source))
    engine._process(capture['id'])
    assert engine.store.drafts_changed() == []
    assert engine.store.db.execute('SELECT COUNT(*) FROM history_imports').fetchone()[0] == 0
