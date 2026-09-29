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


def test_body_is_saved_without_runner_and_export_preserves_it(pipeline):
    engine, store, path = pipeline
    append(path, 'Synthetic NPU case: eager inference returned 8 tokens. Reported result, not a readiness claim.')
    row = process(engine, path, 'first', summary='private raw native answer')
    assert row['summary'] == ''
    docs = store.drafts_changed(generation=engine._settings().generation)
    assert len(docs) == 1
    assert 'returned 8 tokens' in docs[0]['content']
    assert store.db.execute('SELECT COUNT(*) FROM maintenance_attempts').fetchone()[0] == 0
    from mindie_knowledge.loop.export import build_batch
    from mindie_knowledge.loop.documents import parse_entry
    batch_id = build_batch(store, settings=engine._settings())[0]
    batch = store.batch(batch_id)
    payload = json.loads(batch['batch'])
    assert len(payload['files']) == 1
    public = parse_entry(payload['files'][0]['content'].encode())
    assert public['content'] == docs[0]['content']


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
    assert 'second-public-marker' in docs[0]['summary']
    assert 'second-public-marker' in docs[0]['title']
    assert store.cursor(str(path.resolve()))['ok_finish'] == path.stat().st_size


def test_body_failure_rolls_back_cursor_then_retry_succeeds(pipeline, monkeypatch):
    engine, store, path = pipeline
    append(path, 'atomic-public-marker')
    original = store.create_draft
    def fail_after_write(**kwargs):
        original(**kwargs)
        raise OSError('synthetic power loss before commit')
    monkeypatch.setattr(store, 'create_draft', fail_after_write)
    event = engine.capture(session_id='manual-A', turn_id='crash', transcript_path=str(path))
    engine._process(event['id'])
    assert store.cursor(str(path.resolve())) is None
    assert store.drafts_changed() == []
    monkeypatch.setattr(store, 'create_draft', original)
    process(engine, path, 'retry')
    assert store.drafts_changed()[0]['content'].count('atomic-public-marker') == 1


def test_summary_cannot_rewrite_body_and_failure_does_not_block(pipeline):
    engine, store, path = pipeline
    engine.summary_command = [sys.executable, '-c', 'print(\'{"title":"Injected", "summary":"x", "content":"replace body"}\')']
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
    assert store.status()['transcript_summaries'] == {'failed': 1}
    engine.summary_command = [sys.executable, '-c', 'raise Exception("must not retry same version")']
    transcript_capture.summarize_due(engine)
    assert store.transcript_task(task['task_key']) == task
    # A later body version gets exactly one new metadata opportunity.
    engine.summary_command = [sys.executable, '-c', 'print(\'{"title":"Public case", "summary":"Synthetic public observations."}\')']
    append(path, 'new-observation-marker')
    process(engine, path, 'second')
    body = store.drafts_changed()[0]['content']
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    transcript_capture.summarize_due(engine)
    doc = store.drafts_changed()[0]
    assert doc['content'] == body and doc['title'] == 'Public case'
    assert store.transcript_task(task['task_key'])['summary_status'] == 'complete'


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


def test_upgrade_retains_legacy_gap_and_checkpoint_without_body_model(pipeline):
    engine, store, path = pipeline
    append(path, 'public source marker')
    event = engine.capture(session_id='manual-A', turn_id='legacy', transcript_path=str(path))
    row = store.capture_row(event['id'])
    inc = transcript_double.read_material(path, 0, session_id='manual-A', not_before=0)
    region = store.reserve_region(capture_id=row['id'], file_identity=str(path.resolve()),
        identity=inc['identity'], start=inc['start'], finish=inc['end'], region_digest=inc['digest'],
        observed_cursor=None, status='failed', detail='legacy timeout')
    store.schedule_gap_recovery(region, due=time.time())
    engine.agent_command = [sys.executable, '-c', 'raise Exception("must not call")']
    engine._process(row['id'])
    assert store.capture_row(row['id'])['status'] == 'failed'
    assert store.coverage_gaps(str(path.resolve()))
    assert store.drafts_changed() == []
    assert store.db.execute('SELECT COUNT(*) FROM maintenance_attempts').fetchone()[0] == 0
    with pytest.raises(ValueError, match='forbids body model'):
        engine.agent(dict(role='organize'), attempt_id='forbidden', root_hash=row['root_session'])
    attempt = 'organize:' + row['id'] + ':legacy-result'
    engine.budget.reserve(attempt, row['root_session'], 'organize')
    result = json.dumps(dict(entries=[dict(title='old', summary='old', content='model body')]))
    engine.budget.checkpoint(attempt, result, '{}', capture_id=row['id'], capture_status='apply-pending')
    engine._apply_saved(attempt, row)
    assert engine.budget.application(attempt)['status'] == 'held'
    assert engine.budget.application(attempt)['result'] == result
    assert store.drafts_changed() == []


@pytest.mark.parametrize('failure', ['missing', 'exit'])
def test_scanner_recovers_same_notification_without_new_stop(pipeline, monkeypatch, failure):
    engine, store, path = pipeline
    scanner = engine.redactor_executable
    if failure == 'missing':
        engine.redactor_executable = str(path.parent / 'missing-scanner')
    append(path, 'Authorization: Bearer AbCdEf1234567890')
    event = engine.capture(session_id='manual-A', turn_id='bad', transcript_path=str(path))
    with monkeypatch.context() as patcher:
        if failure == 'exit':
            import subprocess
            patcher.setattr('mindie_knowledge.loop.transcript_redaction.subprocess.run',
                lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, b'', b''))
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


def test_summary_receives_whole_body_without_an_input_cap(pipeline, monkeypatch):
    engine, store, path = pipeline
    engine.summary_command = [sys.executable, '-c', 'print(\'{"title":"Case", "summary":"Model claimed full coverage."}\')']
    append(path, 'first marker ' + 'public middle observation ' * 3000 + 'last marker')
    process(engine, path, 'partial')
    before = store.drafts_changed()[0]['content']
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    inputs = []
    def model(command, payload, **kwargs):
        inputs.append(json.loads(payload)['text'])
        return json.dumps(dict(title='Case', summary='Public observations.'))
    monkeypatch.setattr(transcript_capture, 'bounded_run', model)
    transcript_capture.summarize_due(engine)
    doc = store.drafts_changed()[0]
    assert doc['content'] == before
    assert inputs == [before]
    assert doc['summary'] == 'Public observations.'


def wait_until(predicate, seconds=8):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.05)
    assert predicate(), 'expected pipeline outcome did not arrive'


def test_slow_summary_does_not_hold_new_body_and_stale_result_is_discarded(pipeline):
    engine, store, path = pipeline
    marker, release = path.parent / 'summary-started', path.parent / 'summary-release'
    engine.summary_command = [sys.executable, '-c',
        'import pathlib,time; '
        f'pathlib.Path({str(marker)!r}).touch(); '
        f'exec("while not pathlib.Path({str(release)!r}).exists(): time.sleep(.05)"); '
        'print(\'{"title":"Stale result", "summary":"Old observation."}\')']
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
        assert store.drafts_changed()[0]['title'] != 'Stale result'
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
    write_transcript(harness, path, session, [text], time.time() + 2)
    with closing(Store(tmp_path / 'store', 'test')) as store:
        engine = Engine(store, settings_path=settings, admission=admission,
                        transcript_adapter=load_parser(harness), capture_mode='public-transcript',
                        redactor_executable=scanner)
        event = engine.capture(session_id=session, turn_id='long-public', transcript_path=str(path))
        engine._process(event['id'])
        assert store.capture_row(event['id'])['status'] == 'organized'
        assert store.cursor(str(path.resolve()))['ok_finish'] == path.stat().st_size
        doc = store.drafts_changed()[0]
        assert doc['content'] == '### user\n' + text
        from mindie_knowledge.loop.documents import render_entry, parse_entry
        assert parse_entry(render_entry(doc))['content'] == doc['content']
