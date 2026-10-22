"""Business outcomes for deterministic capture, with an actual secret scanner.

Synthetic public messages go through Engine -> Store -> export. Only the
native harness parser is a fixture here; its production conformance cases
live with the Codex adapter. No fake model can prove the body was preserved.
"""
import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import make_admission, write_settings
from mindie_knowledge.filesystem import path_identity
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
    write_settings(settings, consent_choice="contribute", roots=[tmp_path])
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


@pytest.mark.parametrize('handoff', [False, True])
def test_authorized_first_turn_survives_late_runtime_initialization(tmp_path, scanner, handoff):
    """The first body exists before the runtime DB, after its actual authority."""
    from mindie_knowledge.loop.handoff import _accept_row
    from mindie_knowledge.loop.activation import activation_epoch
    now = time.time()
    settings = tmp_path / 'community.json'
    write_settings(settings, consent_choice="contribute", roots=[tmp_path])
    data = json.loads(settings.read_text())
    data['enabled_at'] = now - 120
    settings.write_text(json.dumps(data))
    admission = Admission(tmp_path / 'admission.sqlite3')
    lease = admission.associate('manual-A', project_root=str(tmp_path), not_before=now - 60)
    path = tmp_path / 'native.jsonl'
    records = [dict(type='session_meta', payload=dict(id='manual-A')),
               dict(type='response_item', timestamp=datetime.fromtimestamp(now - 30, timezone.utc).isoformat(),
                    payload=dict(type='message', role='user', content=[
                        dict(type='input_text', text='FIRST_AUTHORIZED_BODY full correction survives')]))]
    path.write_text(''.join(json.dumps(item) + '\n' for item in records))
    with closing(Store(tmp_path / 'store', 'test')) as store:
        engine = Engine(store, settings_path=settings, admission=admission,
                        transcript_adapter=load_parser('codex'), redactor_executable=scanner)
        if handoff:
            received = _accept_row(store.db, namespace='codex', root_hash='a' * 64,
                                  session='manual-A', turn='first', transcript=str(path), summary='',
                                  generation=data['generation'], boundary=now - 60, scope=str(tmp_path),
                                  epoch=activation_epoch(lease['token']), kind='turn', event_key=None)
        else:
            received = engine.capture(session_id='manual-A', turn_id='first', transcript_path=str(path))
        engine._process(received['id'])
        assert store.capture_row(received['id'])['status'] == 'organized'
        assert 'FIRST_AUTHORIZED_BODY full correction survives' in store.drafts_changed()[0]['content']
        assert store.cursor(path_identity(path))['ok_finish'] == path.stat().st_size


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


@pytest.mark.parametrize('failure_point', ['before-invoke', 'returned'])
def test_summary_checks_cached_authority_and_preserves_returned_effect(pipeline, monkeypatch, tmp_path, failure_point):
    from mindie_knowledge.loop import agent_diagnostics
    monkeypatch.setenv('MINDIE_DIAGNOSTICS_ROOT', str(tmp_path / 'diagnostics'))
    engine, store, path = pipeline
    engine.summary_command = summary_command(title='Synthetic call')
    append(path, 'Synthetic full material for an authority-loss probe.')
    process(engine, path, 'first')
    engine.last_activity = 0
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    db_path = store.root / 'state-v4.sqlite3'
    original_commit, original_run = store.db.commit, transcript_capture.bounded_run
    original_exists = Path.exists
    calls, unavailable = [], []
    def authority_exists(candidate):
        # Windows forbids unlinking an open SQLite file. Inject the same
        # unavailable-path observation while retaining the real receipt DB.
        return False if candidate == db_path and unavailable else original_exists(candidate)
    monkeypatch.setattr(Path, 'exists', authority_exists)
    def lose_after_claim_commit():
        original_commit()
        rows = sqlite3.Connection.execute(store.db, "SELECT count(*) FROM material_summary_attempts WHERE status='invoking'").fetchone()[0]
        if failure_point == 'before-invoke' and rows and not unavailable:
            unavailable.append(True)
    def run_with_actual_return(command, payload, **kwargs):
        if '--identity' not in command:
            calls.append(command)
            if failure_point == 'returned':
                save = kwargs['on_result']
                def lose_authority(raw):
                    unavailable.append(True)
                    return save(raw)
                kwargs['on_result'] = lose_authority
        return original_run(command, payload, **kwargs)
    monkeypatch.setattr(store.db, 'commit', lose_after_claim_commit)
    monkeypatch.setattr(transcript_capture, 'bounded_run', run_with_actual_return)
    with pytest.raises(FileNotFoundError) as caught:
        transcript_capture.summarize_due(engine)
    assert len(calls) == int(failure_point == 'returned')
    if failure_point == 'returned':
        assert caught.value.mindie_execution == 'completed'
        assert caught.value.mindie_result_status == 'returned'
        assert any(item['code'] == 'summary_completed_recording_failed'
                   for item in agent_diagnostics.pending()['items'])
    assert sqlite3.Connection.execute(store.db, 'SELECT status FROM material_summary_attempts').fetchone()[0] == 'invoking'


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
    assert store.cursor(path_identity(path)) is None


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
    assert store.cursor(path_identity(path))['ok_finish'] == path.stat().st_size


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
    assert store.cursor(path_identity(path)) is None
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
    assert build_batch(store, settings=engine._settings()) is None
    from mindie_knowledge.materials.summarizer import SummaryLedger
    assert SummaryLedger(store.db).usage_totals()['model_calls'] == 2
    # Explicit retry applies only to the failed older increment. Later indexed
    # material and its paid result are reused; whole-package readiness follows.
    engine.index_queue.retry(doc['entry_id'])
    transcript_capture.summarize_due(engine)
    assert SummaryLedger(store.db).usage_totals()['model_calls'] == 3
    assert store.drafts_changed()[0]['content'] == body
    assert store.transcript_task(task['task_key'])['summary_status'] == 'complete'
    assert build_batch(store, settings=engine._settings()) is not None


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
    assert store.cursor(path_identity(path)) is None
    assert 'AbCdEf1234567890' not in store.capture_row(event['id'])['detail']
    assert store.continuation_reason(event['id']).startswith('public-io:')
    engine.redactor_executable = scanner
    engine.start()
    try:
        wait_until(lambda: bool(store.drafts_changed()))
        assert 'AbCdEf1234567890' not in store.drafts_changed()[0]['content']
        assert store.cursor(path_identity(path))['ok_finish'] == path.stat().st_size
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
        assert options['cancel'] is not None and engine.operations.snapshot()['activity'] > 0
        engine.stop.set()
        engine.operations.cancel('capture')
        assert options['cancel'].is_set()
        raise MaintenanceCancelled('scanner cancelled by owner')
    with monkeypatch.context() as patcher:
        patcher.setattr(transcript_capture, 'prepare_increment', cancelled)
        engine._process(event['id'])
    assert store.cursor(path_identity(path)) is None
    assert store.drafts_changed() == []
    assert store.capture_row(event['id'])['status'] in {'pending', 'deferred'}
    assert store.continuation_reason(event['id']) == 'service stopped; local work retained'
    engine.stop.clear()
    engine.operations.resume()
    engine._process(event['id'])
    assert store.capture_row(event['id'])['status'] == 'organized'
    assert 'Complete public material must survive scanner cancellation.' in store.drafts_changed()[0]['content']



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
               engine.admission.active_lease(session)['activated_at']) + 1
