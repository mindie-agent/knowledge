"""Business outcomes for deterministic capture, with an actual secret scanner.

Synthetic public messages go through Engine -> Store -> export. Only the
native harness parser is a fixture here; its production conformance cases
live with the Codex adapter. No fake model can prove the body was preserved.
"""
import json
import sys
import time
from contextlib import closing
from datetime import datetime, timezone

import pytest

from conftest import make_admission, write_settings
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, digest
from mindie_knowledge.loop.transcript_redaction import install_scanner, redact
from mindie_knowledge.loop import transcript_capture
import transcript_double
from material_worker_fixture import package_body, command as summary_command, answer, identity
from lane_support import load_parser, transcript_path, write_transcript, SESSIONS


@pytest.fixture(scope='module')
def scanner():
    return install_scanner()


@pytest.fixture
def pipeline(tmp_path, scanner):
    settings = tmp_path / 'community.json'
    write_settings(settings, roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path))
    store = Store(tmp_path / 'store', 'test')
    engine = Engine(store, settings_path=settings, admission=admission,
                    transcript_adapter=load_parser('codex'), capture_mode='public-transcript',
                    redactor_executable=scanner)
    path = tmp_path / 'native.jsonl'
    path.write_text(json.dumps(dict(type='session_meta', payload=dict(id='manual-A'))) + '\n', encoding='utf-8')
    yield engine, store, path
    store.close()


def append(path, text):
    record = dict(type='response_item', timestamp=datetime.now(timezone.utc).isoformat(),
                  payload=dict(type='message', role='user', content=[dict(type='input_text', text=text)]))
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')


def process(engine, path, turn, **kwargs):
    result = engine.capture(session_id='manual-A', turn_id=turn, transcript_path=str(path), **kwargs)
    engine._process(result['id'])
    row = engine.store.capture_row(result['id'])
    assert row['status'] == 'organized', row
    return row


def test_capture_saves_body_but_export_waits_for_summary(pipeline):
    engine, store, path = pipeline
    engine.summary_command = summary_command(title='Public case')
    append(path, 'Synthetic NPU case: eager inference returned 8 tokens. Reported result, not a readiness claim.')
    row = process(engine, path, 'first', summary='private raw native answer')
    assert row['summary'] == ''
    docs = store.drafts_changed(generation=engine._settings().generation)
    assert len(docs) == 1
    assert 'returned 8 tokens' in docs[0]['content']
    assert engine.status()['summary_usage']['model_calls'] == 0
    from mindie_knowledge.loop.export import build_batch
    from mindie_knowledge.loop.documents import parse_entry
    assert build_batch(store, settings=engine._settings()) is None
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    transcript_capture.summarize_due(engine)
    batch_id = build_batch(store, settings=engine._settings())[0]
    batch = store.batch(batch_id)
    from mindie_knowledge.materials.publication import load_batch_payload
    from mindie_knowledge.materials.store import validate_package_files
    payload = load_batch_payload(store, batch)
    package = validate_package_files({item['path'].split('/', 2)[2]: item['content'] for item in payload['files']})
    assert package_body(package) == docs[0]['content']


def test_missing_summary_is_configuration_failure_not_publishable_excerpt(pipeline):
    from mindie_knowledge.loop.export import build_batch
    engine, store, path = pipeline
    append(path, 'Stored locally while the configured summary worker is missing.')
    process(engine, path, 'first')
    assert len(store.drafts_changed()) == 1
    assert engine.status()['summary_mode'] == 'configuration-error'
    engine.last_activity = 0
    transcript_capture.summarize_due(engine)
    task = dict(store.db.execute('SELECT * FROM transcript_tasks').fetchone())
    assert task['summary_status'] == 'failed'
    assert not store.has_changed_drafts(generation=engine._settings().generation, ready_only=True)
    assert build_batch(store, settings=engine._settings()) is None


def test_corrupt_page_fails_without_advancing_or_publishing(pipeline):
    engine, store, path = pipeline
    with path.open('ab') as stream:
        stream.write(b'{broken-record}\n')
    append(path, 'public-after-corrupt-page')
    event = engine.capture(session_id='manual-A', turn_id='corrupt-only', transcript_path=str(path))
    engine._process(event['id'])
    row = store.capture_row(event['id'])
    assert row['status'] == 'failed' and 'incomplete transcript page' in row['detail']
    assert store.drafts_changed() == []
    assert store.cursor(str(path.resolve())) is None


def test_restart_and_duplicate_stop_append_one_task_record(pipeline):
    engine, store, path = pipeline
    append(path, 'first-public-marker')
    row = process(engine, path, 'first')
    engine._process(row['id'])
    append(path, 'second-public-marker')
    resumed = Engine(store, settings_path=engine.settings_path, admission=engine.admission,
                     transcript_adapter=load_parser('codex'), capture_mode='public-transcript',
                     redactor_executable=engine.redactor_executable)
    process(resumed, path, 'second')
    docs = store.drafts_changed()
    assert len(docs) == 1
    assert docs[0]['content'].count('first-public-marker') == 1
    assert docs[0]['content'].count('second-public-marker') == 1
    assert docs[0]['title'] == 'Task experience awaiting indexing'
    assert store.db.execute('SELECT count(*) FROM material_batches').fetchone()[0] == 2
    assert store.cursor(str(path.resolve()))['ok_finish'] == path.stat().st_size


def test_body_failure_rolls_back_cursor_then_retry_succeeds(pipeline, monkeypatch):
    engine, store, path = pipeline
    append(path, 'atomic-public-marker')
    original = store.commit_material_increment
    def fail_after_write(**kwargs):
        original(**kwargs)
        raise OSError('synthetic power loss before commit')
    monkeypatch.setattr(store, 'commit_material_increment', fail_after_write)
    event = engine.capture(session_id='manual-A', turn_id='crash', transcript_path=str(path))
    engine._process(event['id'])
    assert store.cursor(str(path.resolve())) is None
    assert store.drafts_changed() == []
    monkeypatch.setattr(store, 'commit_material_increment', original)
    process(engine, path, 'retry')
    assert store.drafts_changed()[0]['content'].count('atomic-public-marker') == 1


def test_summary_cannot_rewrite_body_and_failure_does_not_block(pipeline):
    engine, store, path = pipeline
    engine.summary_command = summary_command(fail=True)
    append(path, 'body-must-survive-marker')
    process(engine, path, 'first')
    before = store.drafts_changed()[0]['content']
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    transcript_capture.summarize_due(engine)
    assert store.drafts_changed()[0]['content'] == before
    task = dict(store.db.execute('SELECT * FROM transcript_tasks').fetchone())
    assert task['summary_status'] == 'failed'
    from mindie_knowledge.loop.export import build_batch
    assert build_batch(store, settings=engine._settings()) is None
    assert store.status()['transcript_summaries'] == {'failed': 1}
    engine.summary_command = [sys.executable, '-c', 'raise Exception("must not retry same version")']
    transcript_capture.summarize_due(engine)
    assert store.transcript_task(task['task_key']) == task
    # A later body version gets exactly one new metadata opportunity.
    engine.summary_command = summary_command(title='Public case')
    append(path, 'new-observation-marker')
    process(engine, path, 'second')
    body = store.drafts_changed()[0]['content']
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    transcript_capture.summarize_due(engine)
    doc = store.drafts_changed()[0]
    assert doc['content'] == body and doc['title'] == 'Public case'
    assert store.transcript_task(task['task_key'])['summary_status'] == 'failed'
    # New evidence cannot silently retry the failed older batch.
    assert store.db.execute("SELECT count(*) FROM material_batches WHERE status='failed'").fetchone()[0] == 1


def test_real_scanner_secrets_unicode_overlap_and_technical_negative_controls(scanner):
    text = '中文\npassword = "Hk8Pm7Wz9Rq2Vt6S" # gitleaks:allow\nAuthorization: Bearer AbCdEf1234567890\nemail=private@private.company\nssh root@10.88.0.9\nC:\\Users\\alice\\work\\run.py\nhttps://hooks.slack.com/services/T00000000/B00000000/abcdefghijklmnopqrstuvwx # gitleaks:allow\n'
    controls = 'torch==2.10.0.post2 vllm==0.11.0 shape=(1, 4096) BF16 8 tokens 哈希 ' + 'abcdef0123456789' * 4
    masked, rules = redact(text + controls, executable=scanner, key=b'a' * 32)
    for secret in ('Hk8Pm7Wz9Rq2Vt6S', 'AbCdEf1234567890', 'private@private.company', '10.88.0.9', 'alice', 'abcdefghijklmnopqrstuvwx'):
        assert secret not in masked
    assert 'secret-slack-webhook-url' in rules
    assert controls in masked
    assert redact(text + controls, executable=scanner, key=b'a' * 32)[0] == masked
    assert redact(masked, executable=scanner, key=b'a' * 32)[0] == masked


@pytest.mark.parametrize('failure', ['scanner', 'interrupted'])
def test_metadata_recovers_the_same_body_without_another_stop(pipeline, failure):
    engine, store, path = pipeline
    engine.summary_command = summary_command(title='Recovered title')
    append(path, 'body-survives-metadata-interruption')
    process(engine, path, 'last-stop')
    before = store.drafts_changed()[0]['content']
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    if failure == 'scanner':
        scanner = engine.redactor_executable
        engine.redactor_executable = str(path.parent / 'unavailable-scanner')
        transcript_capture.summarize_due(engine)
        task = dict(store.db.execute('SELECT * FROM transcript_tasks').fetchone())
        assert task['summary_status'] == 'pending'
        assert task['summary_due'] > time.time()
        assert store.drafts_changed()[0]['content'] == before
        engine.redactor_executable = scanner
    else:
        with store._write_txn():
            store.db.execute("UPDATE transcript_tasks SET summary_status='running'")
    engine.start()
    try:
        with store._write_txn():
            store.db.execute('UPDATE transcript_tasks SET summary_due=0')
        wait_until(lambda: store.drafts_changed()[0]['title'] == 'Recovered title')
        assert store.drafts_changed()[0]['content'] == before
        assert store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0] == 1
    finally:
        engine.shutdown()


def test_dense_private_addresses_and_unicode_line_separators(scanner):
    public = '中文\u2028still one scanner line\n'
    source = public + 'password = "Hk8Pm7Wz9Rq2Vt6S"\n' + ('10.88.0.9 BF16 shape=(1, 4096)\n' * 4000)
    masked, rules = redact(source, executable=scanner, key=b'a' * 32)
    assert public in masked
    assert 'Hk8Pm7Wz9Rq2Vt6S' not in masked and '10.88.0.9' not in masked
    assert masked.count('BF16 shape=(1, 4096)') == 4000
    assert len(set(__import__('re').findall(r'<redacted:ipv4-address:[^>]+>', masked))) == 1


def test_known_workspace_paths_are_masked_in_markdown_and_both_windows_spellings(scanner):
    text = r'[report](D:/private/work/task/report.md) and D:\private\work\task\result.json; keep D:/private/work/task-other/public'
    masked, _ = redact(text, executable=scanner, key=b'a' * 32, private_paths=(r'D:\private\work\task',))
    assert 'D:/private/work/task/report' not in masked and r'D:\private\work\task\result' not in masked
    assert 'report.md' in masked and 'result.json' in masked
    assert 'D:/private/work/task-other/public' in masked


def test_profile_paths_across_file_uri_wsl_and_windows(scanner):
    paths = ('file:///home/alice/work/run.py', '/mnt/c/Users/alice/work/run.py',
             'C:/Users/alice/work/run.py', r'C:\Users\alice\work\run.py')
    source = '\n'.join(paths) + '\nPublic module: torch/nn/functional.py'
    masked, _ = redact(source, executable=scanner, key=b'a' * 32)
    assert 'alice' not in masked
    assert 'torch/nn/functional.py' in masked
    assert redact(masked, executable=scanner, key=b'a' * 32)[0] == masked


def test_workspace_aliases_and_uri_suffixes(scanner):
    for scope in ('D:/private/work/task', r'D:\private\work\task', '/mnt/d/private/work/task'):
        source = 'D:/private/work/task#fragment ' + r'D:\private\work\task?x=1 D:\private\work\task. /mnt/d/private/work/task/result'
        masked, _ = redact(source, executable=scanner, key=b'a' * 32, private_paths=(scope,))
        assert 'private/work/task' not in masked and r'private\work\task' not in masked
    source = 'D:/private/work/task-other D:/private/work/task.extra'
    assert redact(source, executable=scanner, key=b'a' * 32, private_paths=('D:/private/work/task',))[0] == source


@pytest.mark.parametrize('failure', ['missing', 'exit', 'invalid-report'])
def test_scanner_recovers_same_notification_without_new_stop(pipeline, monkeypatch, failure):
    engine, store, path = pipeline
    scanner = engine.redactor_executable
    if failure == 'missing':
        engine.redactor_executable = str(path.parent / 'missing-scanner')
    append(path, 'Authorization: Bearer AbCdEf1234567890')
    event = engine.capture(session_id='manual-A', turn_id='bad', transcript_path=str(path))
    with monkeypatch.context() as patcher:
        if failure in {'exit', 'invalid-report'}:
            from mindie_knowledge.community.common import ProcessResult
            patcher.setattr('mindie_knowledge.loop.transcript_redaction.run_argv',
                lambda *args, **kwargs: ProcessResult(
                    2 if failure == 'exit' else 0, b'not-json', b'', False))
        engine._process(event['id'])
    assert store.drafts_changed() == []
    assert store.cursor(str(path.resolve())) is None
    assert 'AbCdEf1234567890' not in store.capture_row(event['id'])['detail']
    assert store.continuation_reason(event['id']).startswith('public-io:')
    engine.redactor_executable = scanner
    engine.start()
    try:
        wait_until(lambda: bool(store.drafts_changed()))
        assert 'AbCdEf1234567890' not in store.drafts_changed()[0]['content']
        assert store.cursor(str(path.resolve()))['ok_finish'] == path.stat().st_size
        assert store.capture_row(event['id'])['status'] == 'organized'
    finally:
        engine.shutdown()


def test_revoked_pending_summary_does_not_block_next_task(pipeline):
    engine, store, path = pipeline
    engine.summary_command = [sys.executable, '-c', 'raise Exception("must not call after revocation")']
    append(path, 'original public marker')
    process(engine, path, 'first')
    engine.admission.deactivate('manual-A')
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    transcript_capture.summarize_due(engine)
    state = store.db.execute('SELECT summary_status FROM transcript_tasks').fetchone()[0]
    assert state == 'cancelled'
    assert store.drafts_changed()[0]['content'].count('original public marker') == 1


def test_scanner_shutdown_keeps_same_capture_pending_without_advancing_cursor(pipeline, monkeypatch):
    from mindie_knowledge.loop.process import MaintenanceCancelled
    engine, store, path = pipeline
    append(path, 'Complete public material must survive scanner cancellation.')
    event = engine.capture(session_id='manual-A', turn_id='scanner-shutdown', transcript_path=str(path))
    def cancelled(**options):
        assert options['cancel'] is engine._cancel
        engine.stop.set()
        engine._cancel.set()
        raise MaintenanceCancelled('scanner cancelled by owner')
    with monkeypatch.context() as patcher:
        patcher.setattr(transcript_capture, 'prepare_increment', cancelled)
        engine._process(event['id'])
    assert store.cursor(str(path.resolve())) is None
    assert store.drafts_changed() == []
    assert store.capture_row(event['id'])['status'] in {'pending', 'deferred'}
    assert store.continuation_reason(event['id']) == 'service stopped; local work retained'
    engine.stop.clear()
    engine._cancel.clear()
    engine._process(event['id'])
    assert store.capture_row(event['id'])['status'] == 'organized'
    assert 'Complete public material must survive scanner cancellation.' in store.drafts_changed()[0]['content']


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


def wait_until(predicate, seconds=8):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.05)
    assert predicate(), 'expected pipeline outcome did not arrive'


def authorized_message_time(engine, session):
    # Initialization can take longer than a guessed future offset. Construct
    # public messages only after all persisted authorization boundaries exist.
    return max(engine._settings().enabled_at,
               engine.admission.active_lease(session)['activated_at'],
               engine.store.capture_floor) + 1


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
        assert store.cursor(str(path.resolve()))['ok_finish'] == path.stat().st_size
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
@pytest.mark.parametrize('size', [129 * 1024, 1024 * 1024, 10 * 1024 * 1024])
def test_production_parsers_preserve_large_public_messages_through_export(tmp_path, scanner, harness, size):
    session = SESSIONS[harness]
    settings = tmp_path / 'community.json'
    write_settings(settings, roots=[tmp_path])
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
        assert store.cursor(str(path.resolve()))['ok_finish'] == path.stat().st_size
        doc = store.drafts_changed()[0]
        assert doc['content'] == '### user\n' + text + '\n\n'
        assert package_body(store.materials.export_task(doc['entry_id'])) == doc['content']


def test_kimi_lineage_recovers_without_exporting_inherited_history(tmp_path, scanner):
    session = SESSIONS['kimi']
    settings = tmp_path / 'community.json'
    write_settings(settings, roots=[tmp_path])
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
        assert store.cursor(str(path.resolve())) is None
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
    write_settings(settings, roots=[tmp_path])
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
        assert store.cursor(str(path.resolve())) is None
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
    write_settings(settings, roots=[tmp_path])
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
    assert box['store'].cursor(str(box['path'].resolve()))['ok_finish'] == box['path'].stat().st_size
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
    assert box['store'].cursor(str(box['path'].resolve())) is None
    assert box['store'].drafts_changed() == []
