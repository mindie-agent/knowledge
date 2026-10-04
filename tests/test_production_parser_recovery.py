"""Native parser recovery with complete material retained independently of indexing.

The frozen Kimi, Claude Code and Codex parsers consume anonymous records in
real files. An explicit protocol worker counts summary invocations; it is not
model-quality evidence. Old body-rewriting organizer recovery is retired.
"""
from pathlib import Path
import sys
import time

import pytest

from conftest import make_admission, write_settings
from lane_support import PARSER_NAMES, SESSIONS, append_record, load_parser, parser_path, transcript_path, write_transcript
from material_worker_fixture import command as summary_command
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_capture import summarize_due
from mindie_knowledge.loop.transcript_redaction import install_scanner


@pytest.fixture(scope='module')
def scanner():
    return install_scanner()


@pytest.fixture(params=PARSER_NAMES)
def world(tmp_path, request, scanner):
    project = tmp_path / 'project'
    project.mkdir()
    settings = tmp_path / 'community.json'
    write_settings(settings, roots=[project])
    name = request.param
    session = SESSIONS[name]
    admission = Admission(make_admission(tmp_path, project_root=project, session=session))
    parser = load_parser(name)
    assert Path(parser.__file__).resolve() == parser_path(name).resolve()
    path = transcript_path(name, tmp_path / 'logs', session)
    store = Store(tmp_path / 'store', 'test')
    args = dict(settings_path=settings, admission=admission, transcript_adapter=parser,
                redactor_executable=scanner)
    engine = Engine(store, **args)
    when = max(engine._settings().enabled_at, admission.active_lease(session)['activated_at'],
               store.capture_floor) + 1
    write_transcript(name, path, session, ['EARLYCTRL 原始观测; acceptance remains incomplete.'], when)
    box = dict(name=name, session=session, path=path, when=when, store=store, args=args,
               engine=engine, calls=tmp_path / 'calls', root=tmp_path / 'store')
    yield box
    box['store'].close()


def capture(box, turn):
    engine = box['engine']
    result = engine.capture(session_id=box['session'], turn_id=turn, transcript_path=str(box['path']))
    engine._process(result['id'])
    return box['store'].capture_row(result['id'])


def summarize(box, **kw):
    engine = box['engine']
    engine.summary_command = summary_command(calls=box['calls'], **kw)
    engine.last_activity = 0
    with box['store']._write_txn():
        box['store'].db.execute('UPDATE transcript_tasks SET summary_due=0')
    summarize_due(engine)


def reopen(box):
    box['store'].close()
    box['store'] = Store(box['root'], 'test')
    box['engine'] = Engine(box['store'], **box['args'])


def body(box):
    docs = box['store'].drafts_changed()
    assert len(docs) == 1
    return docs[0]['content']


def test_failed_index_preserves_material_and_later_increment_is_independent(world):
    box = world
    assert capture(box, 'first')['status'] == 'organized'
    before = body(box)
    summarize(box, fail=True)
    assert body(box) == before and box['calls'].read_text() == 'x'
    reopen(box)
    summarize(box)
    assert box['calls'].read_text() == 'x'  # terminal failure cannot silently replay
    append_record(box['name'], box['path'], box['session'], 'LATECTRL previous claim withdrawn.', box['when'] + 5)
    assert capture(box, 'second')['status'] == 'organized'
    summarize(box, required='LATECTRL')
    assert box['calls'].read_text() == 'xx'
    assert body(box).count('EARLYCTRL') == body(box).count('LATECTRL') == 1
    statuses = {row[0] for row in box['store'].db.execute('SELECT status FROM material_batches')}
    assert statuses == {'failed', 'complete'}
    assert not box['store'].has_changed_drafts(generation=box['engine']._settings().generation, ready_only=True)


def test_duplicate_stop_after_restart_does_not_append_or_pay_twice(world):
    box = world
    row = capture(box, 'first')
    assert row['status'] == 'organized'
    summarize(box)
    before = body(box)
    reopen(box)
    again = box['engine'].capture(session_id=box['session'], turn_id='first', transcript_path=str(box['path']))
    assert again['duplicate'] and again['id'] == row['id']
    box['engine']._process(again['id'])
    summarize(box)
    assert box['calls'].read_text() == 'x' and body(box) == before
    assert box['store'].db.execute('SELECT count(*) FROM material_batches').fetchone()[0] == 1


def test_incomplete_multibyte_tail_keeps_committed_prefix_then_resumes(world):
    box = world
    assert capture(box, 'first')['status'] == 'organized'
    prefix = box['path'].read_bytes()
    complete = append_record(box['name'], box['path'], box['session'], 'TAILUTF 后续设备完成', box['when'] + 5)
    split = next(i for i, byte in enumerate(complete) if byte >= 0x80)
    box['path'].write_bytes(prefix + complete[:split + 1])
    capture(box, 'tail')
    assert 'EARLYCTRL' in body(box) and 'TAILUTF' not in body(box)
    assert box['store'].cursor(str(box['path'].resolve()))['ok_finish'] == len(prefix)
    reopen(box)
    box['path'].write_bytes(prefix + complete)
    capture(box, 'tail')
    assert body(box).count('EARLYCTRL') == body(box).count('TAILUTF') == 1
    assert box['store'].cursor(str(box['path'].resolve()))['ok_finish'] == box['path'].stat().st_size


def test_unreturned_worker_is_unknown_and_reopen_does_not_repeat(world):
    box = world
    assert capture(box, 'first')['status'] == 'organized'
    # Identity is a read-only protocol call; the execution fails before any receipt.
    source = box['path'].parent / 'unknown_worker.py'
    source.write_text('import json,sys\nfrom pathlib import Path\n'
                      'sys.path.insert(0, ' + repr(str(Path(__file__).parent)) + ')\n'
                      'from material_worker_fixture import identity\n'
                      'if "--identity" in sys.argv:\n print(json.dumps(identity()))\nelse:\n'
                      ' Path(' + repr(str(box['calls'])) + ').write_text("x")\n raise SystemExit(124)\n')
    box['engine'].summary_command = [sys.executable, str(source)]
    box['engine'].last_activity = 0
    with box['store']._write_txn():
        box['store'].db.execute('UPDATE transcript_tasks SET summary_due=0')
    summarize_due(box['engine'])
    assert box['store'].db.execute('SELECT status FROM material_batches').fetchone()[0] == 'outcome_unknown'
    before = body(box)
    reopen(box)
    summarize(box)
    assert box['calls'].read_text() == 'x' and body(box) == before
