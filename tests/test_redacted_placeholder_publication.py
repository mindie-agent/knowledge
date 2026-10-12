"""Closed placeholders stay public; rejection cannot hide a partial delivery."""
from contextlib import closing

import pytest

from conftest import write_settings
from mindie_knowledge import redact
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import ExportQuarantinedError, build_batch
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_redaction import redact as redact_body, install_scanner


def test_generated_host_placeholder_is_scan_clean_and_exported_unchanged(tmp_path):
    text = 'SSH identity <redacted:username-at-host:a1b2c3d4e5f6> remains private.'
    assert redact.scan_text(text) == []
    shared = write_settings(tmp_path / 'sharing.json', roots=[tmp_path])
    with closing(Store(tmp_path / 'data', 'test')) as store:
        doc = store.create_draft(kind='experience', title='Public method', summary='Unverified trial.',
                                 content=text, generation=shared.generation)
        built = build_batch(store, settings=shared)
        assert built[3] == [doc['entry_id']]
        body = next(f['content'] for f in built[2]['files'] if '/blocks/' in f['path'])
        assert text in body
        assert store.quarantined_entries() == {}


@pytest.mark.parametrize('token', [
    '<redacted:username-at-host:a1b2c3d4e5f>',
    '<redacted:username-at-host:a1b2c3d4e5f6a>',
    '<redacted:username-at-host:A1B2C3D4E5F6>',
    '<redacted:username-at-host:a1b2c3d4e5f6',
    'username-at-host:a1b2c3d4e5f6',
])
def test_malformed_or_naked_placeholder_hash_is_still_detected(token):
    assert 'hostname-assignment' in {f.rule for f in redact.scan_text(token)}


def test_placeholder_does_not_suppress_surrounding_or_cross_boundary_findings(monkeypatch):
    token = '<redacted:username-at-host:a1b2c3d4e5f6>'
    text = 'host=' + 'npu' + '-rack7 ' + token + ' server=' + 'npu' + '-rack9'
    assert len([f for f in redact.scan_text(text) if f.rule == 'hostname-assignment']) == 2
    rule = redact.Rule(id='cross-boundary', description='fixture', hint='fixture',
                       finder=lambda value: [(0, len(value))])
    monkeypatch.setattr(redact, 'RULES', (rule,))
    assert redact.scan_text('outside ' + token)[0].rule == 'cross-boundary'


def test_source_redaction_unions_long_claim_over_an_inner_identity():
    identity = 'w' + '00123456'
    tail = '/Run' + 'Case' * 12 + '20261012T031245Z'
    # Chinese prose touches the path, while an employee rule claims the inner
    # account. The longer high-entropy finding must still mask its entire tail.
    text = '结果目录/home/' + identity + tail
    reported = redact.scan_text(text)
    all_claims = redact.scan_text(text, include_overlaps=True)
    assert 'employee-id' in {f.rule for f in reported}
    assert 'credential-high-entropy' in {f.rule for f in all_claims}
    clean, _ = redact_body(text, executable=install_scanner(), key=b'fixture')
    assert identity not in clean and tail not in clean
    assert redact.scan_text(clean) == []
    assert redact_body(clean, executable=install_scanner(), key=b'fixture')[0] == clean


@pytest.mark.parametrize('valid', [False, True])
@pytest.mark.parametrize('remote_outcome', ['submitted', 'unknown'])
def test_publisher_reports_rejection_and_retains_exact_partial_remote_result(tmp_path, valid, remote_outcome):
    shared = write_settings(tmp_path / 'sharing.json', roots=[tmp_path])
    with closing(Store(tmp_path / 'data', 'test')) as store:
        unsafe = store.create_draft(kind='experience', title='Unsafe endpoint', summary='Synthetic.',
                                   content='host=' + 'npu' + '-rack7', generation=shared.generation)
        if valid:
            store.create_draft(kind='experience', title='Public method', summary='Synthetic.',
                               content='The failed trial remains unverified.', generation=shared.generation)
        engine = Engine(store, settings_path=shared.path)
        sent = []
        def submit(batch, *args, **kwargs):
            sent.append(batch)
            if remote_outcome == 'unknown':
                raise OSError('Synthetic uncertain remote write')
            return dict(status='submitted', head_sha='a' * 40, pr_url='https://example.invalid/pull/1')
        engine.community = dict(submit_batch=submit)
        with pytest.raises(ExportQuarantinedError) as rejected:
            engine.publisher.flush()
        assert rejected.value.rejections[0]['entry_id'] == unsafe['entry_id']
        assert any('hostname-assignment' in error for error in engine.errors)
        assert 'npu' + '-rack7' not in str(rejected.value)
        if valid:
            assert len(sent) == 1
            assert rejected.value.publication['status'] == remote_outcome
            row = store.batch(rejected.value.publication['batch_id'])
            assert row['head_sha'] == ('a' * 40 if remote_outcome == 'submitted' else None)
        else:
            assert sent == [] and rejected.value.publication is None
        engine.publisher.flush()
        assert len(sent) == int(valid)  # no second write on an idle tick
