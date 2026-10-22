"""Paged, long-batch and restart outcomes for public transcript capture.

These cases drive the same Engine -> Store -> export path as the core public
transcript suite; the parser remains the only fixture. Pagination is tested
with a one-record page budget so an interrupted page is observable.
"""
import json
import time
from contextlib import closing

import pytest

from conftest import make_admission, write_settings
from mindie_knowledge.filesystem import path_identity
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop import transcript_capture
from mindie_knowledge.loop.transcript_redaction import install_scanner
from material_worker_fixture import package_body, command as summary_command, answer, identity
from lane_support import load_parser, transcript_path, write_transcript, SESSIONS
from test_public_transcript import (
    append,
    authorized_message_time,
    process,
    wait_until,
)


@pytest.fixture(scope="module")
def scanner():
    return install_scanner()


@pytest.fixture
def pipeline(tmp_path, scanner):
    settings = tmp_path / "community.json"
    write_settings(settings, consent_choice="contribute", roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path))
    store = Store(tmp_path / "store", "test")
    engine = Engine(store, settings_path=settings, admission=admission,
                    transcript_adapter=load_parser("codex"), capture_mode="public-transcript",
                    redactor_executable=scanner)
    path = tmp_path / "native.jsonl"
    path.write_text(json.dumps(dict(type="session_meta", payload=dict(id="manual-A"))) + "\n", encoding="utf-8")
    yield engine, store, path
    store.close()


def test_k3_long_task_batches_cover_every_byte_without_old_body_replay(pipeline, monkeypatch):
    engine, store, path = pipeline
    engine.summary_command = ['fixture']
    append(path, 'first marker ' + 'public middle observation ' * 3000 + 'last marker')
    process(engine, path, 'partial')
    before = store.drafts_changed()[0]['content']
    engine.last_activity = 0
    inputs = []
    def model(command, payload, **kwargs):
        if '--identity' in command:
            return json.dumps(identity())
        request = json.loads(payload)
        inputs.extend(b['text'] for b in request['blocks'])
        return json.dumps(answer(request, 'Case', 'Public observations.'))
    monkeypatch.setattr(transcript_capture, 'bounded_run', model)
    for _ in range(5):
        transcript_capture.summarize_due(engine)
    doc = store.drafts_changed()[0]
    assert doc['content'] == before
    assert ''.join(inputs) == before
    assert doc['summary'] == 'Public observations.'



def test_slow_summary_does_not_hold_new_body_and_stale_result_is_discarded(pipeline):
    engine, store, path = pipeline
    marker, release = path.parent / 'summary-started', path.parent / 'summary-release'
    engine.summary_command = summary_command(title='Indexed first block', started=marker, release=release)
    append(path, 'first-public-marker')
    process(engine, path, 'first')
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    engine.start()
    try:
        wait_until(marker.exists)
        append(path, 'second-public-marker')
        engine.capture(session_id='manual-A', turn_id='second', transcript_path=str(path))
        wait_until(lambda: 'second-public-marker' in store.drafts_changed()[0]['content'], seconds=3)
        assert not release.exists()
        assert store.cursor(path_identity(path))['ok_finish'] == path.stat().st_size
        # Settle is long enough to inspect completion of the old version alone.
        engine.last_activity = time.monotonic() + 30
        release.touch()
        wait_until(lambda: engine.status()['activity'] == 0)
        task = store.materials.read_task(store.drafts_changed()[0]['entry_id'])
        assert [block['indexed'] for block in task['blocks']] == [True, False]
        assert not store.summary_ready(store.drafts_changed()[0])
    finally:
        release.touch()
        engine.shutdown()


def test_repeated_enable_keeps_pending_material_and_authorization(pipeline):
    from mindie_knowledge.loop.settings import CommunityWriteContext
    engine, store, path = pipeline
    append(path, 'accepted-before-repeated-enable')
    event = engine.capture(session_id='manual-A', turn_id='pending', transcript_path=str(path))
    settings_path = engine.settings_path
    with CommunityWriteContext(settings_path) as context:
        original = context.read(settings_path)
        current = context.configure(settings_path, dict(original.raw, enabled=True))
    before = settings_path.read_bytes()
    with CommunityWriteContext(settings_path) as context:
        repeated = context.configure(settings_path, dict(current.raw, enabled=True))
    assert repeated.generation == original.generation
    assert repeated.enabled_at == original.enabled_at
    assert settings_path.read_bytes() == before
    engine.revoke_stale()
    engine._process(event['id'])
    assert 'accepted-before-repeated-enable' in store.drafts_changed()[0]['content']


@pytest.mark.parametrize('harness', ['codex', 'kimi', 'cc'])
@pytest.mark.parametrize('size', [
    129 * 1024,
    pytest.param(
        10 * 1024 * 1024,
        marks=[
            pytest.mark.benchmark,
            pytest.mark.skipif(
                __import__('os').environ.get('MINDIE_RUN_BENCHMARK') != '1',
                reason='10MB benchmark; set MINDIE_RUN_BENCHMARK=1 to run',
            ),
        ],
    ),
])
def test_production_parsers_preserve_large_public_messages_through_export(tmp_path, scanner, harness, size):
    session = SESSIONS[harness]
    settings = tmp_path / 'community.json'
    write_settings(settings, consent_choice="contribute", roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path, session=session))
    path = transcript_path(harness, tmp_path, session)
    # Distinct head/middle/tail and total bytes catch clipping, skipped records,
    # and dropped multibyte data. The only injected private value is the token.
    text = 'begin-public\n' + ('公开进度。\n' * (size // 16)) + '\nend-public'
    with closing(Store(tmp_path / 'store', 'test')) as store:
        engine = Engine(store, settings_path=settings, admission=admission,
                        transcript_adapter=load_parser(harness), capture_mode='public-transcript',
                        redactor_executable=scanner)
        write_transcript(harness, path, session, [text], authorized_message_time(engine, session))
        event = engine.capture(session_id=session, turn_id='long-public', transcript_path=str(path))
        # The worker may yield between pages or defer a temporary scanner
        # failure. Acceptance is eventual complete persistence from this one
        # Stop, not an assumption that the first synchronous tick finishes.
        engine.start()
        try:
            # The scanner has no product deadline. This watchdog checks eventual
            # complete persistence of large messages, not an eight-second SLA;
            # allow bounded time for parsing and durable material writes too.
            wait_until(lambda: store.capture_row(event['id'])['status'] == 'organized', seconds=60)
        except AssertionError:
            pytest.fail(str(store.capture_row(event['id'])))
        finally:
            engine.shutdown()
        assert store.cursor(path_identity(path))['ok_finish'] == path.stat().st_size
        doc = store.drafts_changed()[0]
        assert doc['content'] == '### user\n' + text + '\n\n'
        assert package_body(store.materials.export_task(doc['entry_id'])) == doc['content']


def test_kimi_lineage_recovers_without_exporting_inherited_history(tmp_path, scanner):
    session = SESSIONS['kimi']
    settings = tmp_path / 'community.json'
    write_settings(settings, consent_choice="contribute", roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path, session=session))
    path = transcript_path('kimi', tmp_path, session)
    from lane_support import encode_record
    state = path.parents[2] / 'state.json'
    with closing(Store(tmp_path / 'store', 'test')) as store:
        engine = Engine(store, settings_path=settings, admission=admission,
                        transcript_adapter=load_parser('kimi'), capture_mode='public-transcript',
                        redactor_executable=scanner)
        when = authorized_message_time(engine, session)
        path.write_bytes(encode_record('kimi', session, 'inherited-parent-marker', when)
                         + encode_record('kimi', session, 'new-child-marker', when + 2))
        state.unlink()
        event = engine.capture(session_id=session, turn_id='lineage', transcript_path=str(path))
        engine._process(event['id'])
        assert store.drafts_changed() == []
        assert store.cursor(path_identity(path)) is None
        assert store.continuation_reason(event['id']).startswith('public-io:')
        state.write_text(json.dumps(dict(forkedFrom='parent', createdAt=int((when + 1) * 1000))), encoding='utf-8')
        engine.start()
        try:
            wait_until(lambda: bool(store.drafts_changed()))
            body = store.drafts_changed()[0]['content']
            assert 'new-child-marker' in body
            assert 'inherited-parent-marker' not in body
            assert store.capture_row(event['id'])['status'] == 'organized'
        finally:
            engine.shutdown()


@pytest.mark.parametrize('harness', ['codex', 'kimi', 'cc'])
@pytest.mark.parametrize('bad', [b'{"bad":broken}\n', b'null\n', b'\xff\n'])
def test_corrupt_record_blocks_incomplete_page_without_losing_source(tmp_path, scanner, harness, bad):
    from lane_support import encode_record
    session = SESSIONS[harness]
    settings = tmp_path / 'community.json'
    write_settings(settings, consent_choice="contribute", roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path, session=session))
    path = transcript_path(harness, tmp_path, session)
    with closing(Store(tmp_path / 'store', 'test')) as store:
        engine = Engine(store, settings_path=settings, admission=admission,
                        transcript_adapter=load_parser(harness), capture_mode='public-transcript',
                        redactor_executable=scanner)
        when = authorized_message_time(engine, session)
        write_transcript(harness, path, session, ['before-bad-marker'], when)
        with path.open('ab') as stream:
            stream.write(bad)
            stream.write(encode_record(harness, session, 'after-bad-marker', when + 2))
        event = engine.capture(session_id=session, turn_id='broken-line', transcript_path=str(path))
        engine._process(event['id'])
        row = store.capture_row(event['id'])
        assert row['status'] == 'failed', row
        discarded = json.loads(row['detail'])['records']
        assert len(discarded) == 1 and discarded[0]['reason'] == 'invalid record'
        assert discarded[0]['end'] - discarded[0]['start'] == len(bad)
        assert store.cursor(path_identity(path)) is None
        assert store.drafts_changed() == []
        assert bad in path.read_bytes()
        assert b'before-bad-marker' in path.read_bytes() and b'after-bad-marker' in path.read_bytes()


def test_summary_of_previous_body_does_not_release_a_new_body(pipeline):
    from mindie_knowledge.loop.export import build_batch
    engine, store, path = pipeline
    engine.summary_command = summary_command(title='Public case')
    append(path, 'first result')
    process(engine, path, 'first')
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    transcript_capture.summarize_due(engine)
    original = store.drafts_changed()[0]
    assert store.summary_ready(original)
    changed, _ = store.append_observation(original['entry_id'], 'new evidence', marker='b' * 32,
                                           generation=engine._settings().generation)
    assert not store.summary_ready(changed)
    assert build_batch(store, settings=engine._settings()) is None


def test_k3_live_split_secret_carries_only_scanner_state_between_stops(pipeline):
    engine, store, path = pipeline
    append(path, 'K3-01 initial adaptation\n-----BEGIN RSA PRIVATE KEY-----\nfirst-secret')
    process(engine, path, 'secret-start')
    append(path, 'SPLIT_SECRET_CANARY\n-----END EC PRIVATE KEY-----\nstill-secret')
    process(engine, path, 'wrong-end')
    append(path, 'FINAL_SECRET_CANARY\n-----END RSA PRIVATE KEY-----\npublic precision correction')
    process(engine, path, 'correct-end')
    body = store.drafts_changed()[0]['content']
    assert 'public precision correction' in body
    assert all(value not in body for value in ('first-secret', 'SPLIT_SECRET_CANARY', 'still-secret', 'FINAL_SECRET_CANARY'))
    state = store.db.execute('SELECT redaction_state FROM material_streams').fetchone()[0]
    assert 'secret' not in state.lower() and 'PRIVATE KEY' not in state


@pytest.fixture
def paged_capture(tmp_path, scanner, monkeypatch):
    """Real Kimi parser, with a one-record page budget for deterministic yields."""
    session = SESSIONS['kimi']
    settings = tmp_path / 'community.json'
    write_settings(settings, consent_choice="contribute", roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path, session=session))
    path = transcript_path('kimi', tmp_path, session)
    parser = load_parser('kimi')
    read_material = parser.read_material
    monkeypatch.setattr(parser, 'read_material',
                        lambda *args, **kwargs: read_material(*args, **kwargs, max_scan_bytes=1))
    root = tmp_path / 'store'
    store = Store(root, 'test')
    args = dict(settings_path=settings, admission=admission, transcript_adapter=parser,
                redactor_executable=scanner)
    box = dict(store=store, engine=Engine(store, **args), root=root, args=args,
               path=path, session=session)
    yield box
    box['store'].close()


def append_kimi_noise(box):
    record = dict(type='context.append_message', time=int(time.time() * 1000),
                  message=dict(role='assistant', content=[]))
    with box['path'].open('ab') as stream:
        stream.write(json.dumps(record).encode() + b'\n')


def reopen_capture(box):
    box['store'].close()
    box['store'] = Store(box['root'], 'test')
    box['engine'] = Engine(box['store'], **box['args'])


def begin_paged_body(box):
    write_transcript('kimi', box['path'], box['session'], ['event-body-canary'],
                     authorized_message_time(box['engine'], box['session']))
    append_kimi_noise(box)
    event = box['engine'].capture(session_id=box['session'], turn_id='paged',
                                  transcript_path=str(box['path']), harness='kimi')
    box['engine']._process(event['id'])
    assert box['store'].capture_row(event['id'])['status'] == 'pending'
    assert box['store'].continuation_reason(event['id']) == 'more public transcript bytes'
    return event


@pytest.mark.parametrize('restart', [False, True])
def test_body_event_keeps_receipt_through_noise_eof_and_duplicate_handoff(paged_capture, restart, monkeypatch):
    from mindie_knowledge.loop.cli import capture_hook
    box = paged_capture
    event = begin_paged_body(box)
    receipt = box['store'].capture_material_receipt(event['id'])
    assert receipt['body_model_calls'] == 0
    assert box['store'].status()['captures'][0]['material_receipt'] == receipt
    if restart:
        reopen_capture(box)
    for _ in range(4):
        box['engine']._process(event['id'])
    row = box['store'].capture_row(event['id'])
    assert row['status'] == 'organized'
    assert json.loads(row['detail']) == receipt
    assert box['store'].capture_material_receipt(event['id']) == receipt
    assert box['store'].drafts_changed()[0]['content'] == '### user\nevent-body-canary\n\n'
    assert box['store'].cursor(path_identity(box['path']))['ok_finish'] == box['path'].stat().st_size
    assert box['engine'].status()['summary_usage']['model_calls'] == 0
    config = box['root'].parent / 'engine.json'
    config.write_text(json.dumps(dict(root=str(box['root']), domain='test',
        community_config=str(box['args']['settings_path']),
        admission_path=str(box['args']['admission'].path), capture_mode='public-transcript',
        redactor_executable=box['args']['redactor_executable'],
        transcript_adapter=box['args']['transcript_adapter'].__file__)), encoding='utf-8')
    def no_wake(*args, **kwargs):
        raise AssertionError('completed duplicate must not start a service')
    monkeypatch.setattr('mindie_knowledge.loop.handoff.request_wake', no_wake)
    result = capture_hook(config, dict(hook_event_name='Stop', identity_kind='turn',
        session_id=box['session'], turn_id='paged', harness='kimi',
        mindie_activation=box['args']['admission'].active_lease(box['session'])['token'],
        transcript_path=str(box['path']), budget_seconds=0.8))
    assert result['stage'] == 'organized' and result['duplicate'] is True
    assert result['capture_id'] == event['id']
    assert json.loads(box['store'].capture_row(event['id'])['detail']) == receipt


@pytest.mark.parametrize('prior_body', [False, True])
def test_noise_only_event_does_not_borrow_another_events_body(paged_capture, prior_body):
    box = paged_capture
    box['path'].write_bytes(b'')
    if prior_body:
        write_transcript('kimi', box['path'], box['session'], ['earlier-event-body'],
                         authorized_message_time(box['engine'], box['session']))
        earlier = box['engine'].capture(session_id=box['session'], turn_id='earlier',
                                        transcript_path=str(box['path']), harness='kimi')
        box['engine']._process(earlier['id'])
        assert box['store'].capture_material_receipt(earlier['id']) is not None
    append_kimi_noise(box)
    event = box['engine'].capture(session_id=box['session'], turn_id='noise-only',
                                  transcript_path=str(box['path']), harness='kimi')
    for _ in range(4):
        box['engine']._process(event['id'])
    assert box['store'].capture_row(event['id'])['status'] == 'no-new-material'
    assert box['store'].capture_material_receipt(event['id']) is None
    assert len(box['store'].drafts_changed()) == int(prior_body)
    assert box['engine'].status()['summary_usage']['model_calls'] == 0


def test_committed_body_and_later_parser_failure_are_both_visible_after_restart(paged_capture, monkeypatch):
    box = paged_capture
    event = begin_paged_body(box)
    receipt = box['store'].capture_material_receipt(event['id'])
    read_material = box['args']['transcript_adapter'].read_material
    def fail_tail(*args, **kwargs):
        raise ValueError('synthetic tail parser failure')
    monkeypatch.setattr(box['args']['transcript_adapter'], 'read_material', fail_tail)
    box['engine']._process(event['id'])
    reopen_capture(box)
    status = box['store'].status()['captures'][0]
    assert status['status'] == 'failed'
    assert status['detail'] == 'ValueError: synthetic tail parser failure'
    assert status['material_receipt'] == receipt
    assert box['store'].drafts_changed()[0]['content'] == '### user\nevent-body-canary\n\n'
    # Existing internal requeue, not a public retry API or an automatic retry.
    monkeypatch.setattr(box['args']['transcript_adapter'], 'read_material', read_material)
    box['store'].defer_capture(event['id'], due=0, reason='explicit local requeue')
    for _ in range(4):
        box['engine']._process(event['id'])
    assert box['store'].capture_row(event['id'])['status'] == 'organized'
    assert json.loads(box['store'].capture_row(event['id'])['detail']) == receipt
    assert box['engine'].status()['summary_usage']['model_calls'] == 0


@pytest.mark.parametrize('invalid_receipt', ['{broken', '{"refs":[]}'])
def test_invalid_saved_capture_receipt_is_visible_and_cannot_become_empty(paged_capture, invalid_receipt):
    box = paged_capture
    event = begin_paged_body(box)
    with box['store']._write_txn():
        box['store'].db.execute('UPDATE state SET value=? WHERE key=?',
                                (invalid_receipt, 'capture-material:' + event['id']))
    box['store'].mark_capture(event['id'], 'failed', 'original independent page failure')
    status = box['store'].status()['captures'][0]
    assert status['detail'] == 'original independent page failure'
    assert 'invalid stored capture material receipt' in status['material_receipt_error']
    box['store'].defer_capture(event['id'], due=0, reason='explicit local requeue')
    for _ in range(4):
        box['engine']._process(event['id'])
    assert box['store'].capture_row(event['id'])['status'] == 'failed'
    assert 'invalid stored capture material receipt' in box['store'].capture_row(event['id'])['detail']
    assert box['store'].drafts_changed()[0]['content'] == '### user\nevent-body-canary\n\n'


def test_capture_receipt_failure_rolls_back_body_and_cursor(paged_capture, monkeypatch):
    box = paged_capture
    original = box['store'].record_capture_material
    def fail_after_receipt(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('synthetic receipt commit interruption')
    monkeypatch.setattr(box['store'], 'record_capture_material', fail_after_receipt)
    event = box['engine'].capture(
        session_id=box['session'], turn_id='receipt-failure',
        transcript_path=str(box['path']), harness='kimi')
    write_transcript('kimi', box['path'], box['session'], ['receipt-rollback-canary'],
                     authorized_message_time(box['engine'], box['session']))
    box['engine']._process(event['id'])
    assert box['store'].capture_row(event['id'])['status'] == 'pending'
    assert box['store'].continuation_reason(event['id']).startswith('public-io:')
    assert box['store'].capture_material_receipt(event['id']) is None
    assert box['store'].cursor(path_identity(box['path'])) is None
    assert box['store'].drafts_changed() == []
