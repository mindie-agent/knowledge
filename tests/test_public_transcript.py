"""Business outcomes for deterministic capture, with an actual secret scanner.

Synthetic public messages go through Engine -> Store -> export. Only the
native harness parser is a fixture here; its production conformance cases
live with the Codex adapter. No fake model can prove the body was preserved.
"""
import json
import sys
import time
from datetime import datetime, timezone

import pytest

from conftest import make_admission, write_settings
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, digest
from mindie_knowledge.loop.transcript_redaction import install_scanner, redact
from mindie_knowledge.loop import transcript_capture
import transcript_double


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
                    transcript_adapter=transcript_double, capture_mode='public-transcript',
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


def process(engine, path, turn):
    result = engine.capture(session_id='manual-A', turn_id=turn, transcript_path=str(path))
    engine._process(result['id'])
    row = engine.store.capture_row(result['id'])
    assert row['status'] == 'organized', row
    return row


def test_body_is_saved_without_runner_and_export_preserves_it(pipeline):
    engine, store, path = pipeline
    append(path, 'Synthetic NPU case: eager inference returned 8 tokens. Reported result, not a readiness claim.')
    process(engine, path, 'first')
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
                     transcript_adapter=transcript_double, capture_mode='public-transcript',
                     redactor_executable=engine.redactor_executable)
    process(resumed, path, 'second')
    docs = store.drafts_changed()
    assert len(docs) == 1
    assert docs[0]['content'].count('first-public-marker') == 1
    assert docs[0]['content'].count('second-public-marker') == 1
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
    text = '中文\npassword = "Hk8Pm7Wz9Rq2Vt6S" # gitleaks:allow\nAuthorization: Bearer AbCdEf1234567890\nemail=private@private.company\nssh root@10.88.0.9\nC:\\Users\\alice\\work\\run.py\n'
    controls = 'torch==2.10.0.post2 vllm==0.11.0 shape=(1, 4096) BF16 8 tokens 哈希 ' + 'abcdef0123456789' * 4
    masked, rules = redact(text + controls, executable=scanner, key=b'a' * 32)
    for secret in ('Hk8Pm7Wz9Rq2Vt6S', 'AbCdEf1234567890', 'private@private.company', '10.88.0.9', 'alice'):
        assert secret not in masked
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


def test_scanner_failure_never_consumes_input(pipeline):
    engine, store, path = pipeline
    engine.redactor_executable = str(path.parent / 'missing-scanner')
    append(path, 'Authorization: Bearer AbCdEf1234567890')
    event = engine.capture(session_id='manual-A', turn_id='bad', transcript_path=str(path))
    engine._process(event['id'])
    assert store.drafts_changed() == []
    assert store.cursor(str(path.resolve())) is None
    assert 'AbCdEf1234567890' not in store.capture_row(event['id'])['detail']


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


def test_partial_summary_is_labeled_by_code_and_retains_whole_body(pipeline, monkeypatch):
    engine, store, path = pipeline
    engine.summary_command = [sys.executable, '-c', 'print(\'{"title":"Case", "summary":"Model claimed full coverage."}\')']
    monkeypatch.setattr(transcript_capture, 'SUMMARY_INPUT_BYTES', 64)
    append(path, 'first marker ' + 'public middle observation ' * 30 + 'last marker')
    process(engine, path, 'partial')
    before = store.drafts_changed()[0]['content']
    engine.last_activity = time.monotonic() - 10
    with store._write_txn():
        store.db.execute('UPDATE transcript_tasks SET summary_due=0')
    transcript_capture.summarize_due(engine)
    doc = store.drafts_changed()[0]
    assert doc['content'] == before
    assert doc['summary'].startswith('Excerpt summary (partial source): ')
