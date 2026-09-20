"""Bounded Codex JSONL transcript reader: mechanism tests on controlled files.

These are filesystem mechanism tests with structurally representative files,
clearly not model outcomes and not evidence from real private sessions.
"""

import json
import os

from mindie_knowledge.loop import transcript


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            if isinstance(record, str):
                stream.write(record)
            else:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def message(role, text, **extra):
    ctype = "input_text" if role == "user" else "output_text"
    payload = dict(type="message", role=role,
                   content=[dict(type=ctype, text=text)])
    payload.update(extra)
    return {"timestamp": "2026-09-20T01:00:00Z", "type": "response_item",
            "payload": payload}


def meta(session="task-1"):
    return {"type": "session_meta", "payload": {"id": session, "cwd": "/work"}}


def test_increment_from_zero_extracts_public_records(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [
        meta(),
        message("user", "Investigate the device mapping failure"),
        {"type": "response_item", "payload": {"type": "reasoning", "text": "hidden"}},
        message("assistant", "The container numbers devices from zero"),
        {"type": "response_item", "payload": {"type": "function_call",
                                              "name": "shell", "arguments": "npu-smi"}},
        {"type": "response_item", "payload": {"type": "function_call_output",
                                              "output": "device 8 mapped"}},
        {"type": "response_item", "payload": {"type": "message", "role": "system",
                                              "content": [{"type": "input_text",
                                                           "text": "developer secret"}]}},
    ])
    inc = transcript.read_increment(str(path), 0, session_id="task-1")
    assert inc["status"] == "ok"
    assert "device mapping" in inc["text"]
    assert "zero" in inc["text"] and "npu-smi" in inc["text"]
    assert "hidden" not in inc["text"] and "developer secret" not in inc["text"]
    assert inc["end"] == path.stat().st_size and inc["session_match"] is True


def test_nonzero_offset_reads_only_the_new_region(tmp_path):
    """Repeated real increments: no loop, exact byte ranges, stable digests."""
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [meta(), message("user", "first turn")])
    first_size = path.stat().st_size
    inc1 = transcript.read_increment(str(path), 0, session_id="task-1")
    assert inc1["end"] == first_size
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(message("assistant", "second turn")) + "\n")
    inc2 = transcript.read_increment(str(path), inc1["end"], session_id="task-1")
    assert inc2["status"] == "ok" and inc2["start"] == inc1["end"]
    assert "second turn" in inc2["text"] and "first turn" not in inc2["text"]
    assert inc2["end"] == path.stat().st_size
    inc3 = transcript.read_increment(str(path), inc2["end"], session_id="task-1")
    assert inc3["status"] == "unchanged" and inc3["end"] == inc2["end"]


def test_partial_trailing_record_is_not_consumed(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [meta(), message("user", "complete")])
    complete_end = path.stat().st_size
    with open(path, "ab") as stream:
        stream.write(b'{"type":"response_item","payload":{"type":"message","ro')
    inc = transcript.read_increment(str(path), 0, session_id="task-1")
    assert inc["end"] == complete_end and inc["partial"] is True
    assert "complete" in inc["text"]


def test_analysis_channel_is_private_never_extracted(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [
        meta(),
        message("assistant", "PRIVATE chain draft", channel="analysis"),
        message("assistant", "public answer", channel="final"),
        message("assistant", "unspecified channel answer"),
    ])
    inc = transcript.read_increment(str(path), 0, session_id="task-1")
    assert "PRIVATE" not in inc["text"]
    assert "public answer" in inc["text"]


def test_unknown_format_reports_summary_only(tmp_path):
    path = tmp_path / "other.jsonl"
    write_jsonl(path, [{"foo": 1}, 'not json at all\n'])
    inc = transcript.read_increment(str(path), 0)
    assert inc["status"] == "unknown-format"
    assert inc["text"] == "" and inc["end"] > 0


def test_replacement_and_truncation_stop_the_segment(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [meta(), message("user", "original long body" * 20)])
    identity = transcript.identify(str(path))
    size = path.stat().st_size
    write_jsonl(path, [meta(), message("user", "short")])
    inc = transcript.read_increment(str(path), size, expected=identity)
    assert inc["status"] == "replaced"  # truncated: size < start
    os.unlink(path)
    write_jsonl(path, [meta(), message("user", "new file body")])
    inc = transcript.read_increment(str(path), 0, expected=identity)
    assert inc["status"] == "replaced"


def test_append_preserves_anchored_identity(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [meta(), message("user", "first turn")])
    identity = transcript.identify(str(path))
    assert identity.anchor_len > 0 and len(identity.anchor_digest) == 64
    with open(path, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(message("assistant", "appended turn")) + "\n")
    inc = transcript.read_increment(
        str(path), identity.size, session_id="task-1", expected=identity
    )
    assert inc["status"] == "ok" and "appended turn" in inc["text"]
    restored = transcript.FileIdentity.unserialize(identity.serialize(), identity.path)
    assert restored is not None and transcript.same_file(
        restored, transcript.identify(str(path))
    )


def test_inplace_prefix_rewrite_same_inode_stops(tmp_path):
    """Same inode, equal/larger size, rewritten prefix: the anchor decides."""
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [meta(), message("user", "original body that is long enough" * 4)])
    identity = transcript.identify(str(path))
    stat_before = os.stat(path)
    with open(path, "r+", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(message("user", "REWRITTEN prefix content")) + "\n")
    assert os.stat(path).st_ino == stat_before.st_ino
    assert path.stat().st_size >= 0
    inc = transcript.read_increment(str(path), 0, expected=identity)
    assert inc["status"] == "replaced"


def test_malformed_persisted_identity_fails_closed(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [meta(), message("user", "body")])
    assert transcript.FileIdentity.unserialize("not json", str(path)) is None
    assert transcript.FileIdentity.unserialize(
        '{"dev":1,"ino":2,"anchor_len":0,"anchor_digest":"x" * 64}', str(path)
    ) is None
    identity = transcript.identify(str(path))
    assert not transcript.same_file(None, identity)
    assert not transcript.same_file(identity, None)


def test_foreign_task_transcript_is_not_read(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [meta("other-task"), message("user", "foreign private text")])
    inc = transcript.read_increment(str(path), 0, session_id="task-1")
    assert inc["status"] == "wrong-task"
    assert "foreign private text" not in inc["text"]


def test_authorization_boundary_filters_history(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, [
        {"timestamp": "2026-09-19T01:00:00Z", **{k: v for k, v in message("user", "before enable").items() if k != "timestamp"}},
        message("user", "after enable"),
    ])
    from datetime import datetime, timezone

    boundary = datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp()
    inc = transcript.read_increment(str(path), 0, not_before=boundary)
    assert "after enable" in inc["text"]
    assert "before enable" not in inc["text"]


def test_custom_tools_public_phases_and_noise_are_recognized(tmp_path):
    path=tmp_path/'native.jsonl'
    write_jsonl(path,[meta(),message('assistant','private phase',phase='analysis'),
        message('assistant','public final',phase='final_answer'),
        {'type':'response_item','payload':{'type':'custom_tool_call','name':'functions.exec','call_id':'c1','input':'await tools.exec_command({cmd:"npu-smi"})'}},
        {'type':'response_item','payload':{'type':'custom_tool_call_output','call_id':'c1','output':'actual bounded output'}},
        {'type':'event_msg','payload':{'type':'token_count','secret':'not public'}}])
    inc=transcript.read_material(path,0,session_id='task-1')
    assert 'public final' in inc['text'] and 'npu-smi' in inc['text']
    assert 'call_id=c1' in inc['text'] and 'actual bounded output' in inc['text']
    assert 'private phase' not in inc['text'] and 'not public' not in inc['text']
    old=path.stat().st_size
    with path.open('a') as f:
        f.write(json.dumps({'type':'event_msg','payload':{'type':'token_count'}})+'\n')
    noise=transcript.read_material(path,old,session_id='task-1')
    assert noise['status']=='ok' and not noise['text'] and noise['end']==path.stat().st_size


def test_full_text_envelope_does_not_consume_the_next_record(tmp_path):
    path=tmp_path/'native.jsonl'
    write_jsonl(path,[meta()]+[message('user',f'unique-{i} '+ 'x'*10000) for i in range(9)])
    cursor=0; texts=[]
    for _ in range(10):
        inc=transcript.read_material(path,cursor,session_id='task-1',max_text_bytes=16384)
        texts.append(inc['text']);assert inc['end']>cursor
        assert inc['digest']==__import__('hashlib').sha256(path.read_bytes()[cursor:inc['end']]).hexdigest()
        cursor=inc['end']
        if not inc['more']:break
    assert cursor==path.stat().st_size
    for i in range(9):assert '\n'.join(texts).count(f'unique-{i}')==1


def test_foreign_identity_is_checked_at_nonzero_offset(tmp_path):
    path=tmp_path/'native.jsonl';write_jsonl(path,[meta('foreign'),message('user','private')])
    offset=path.read_bytes().index(b'\n')+1
    inc=transcript.read_material(path,offset,session_id='task-1')
    assert inc['status']=='wrong-task' and not inc['text']


def test_fork_inherited_parent_material_is_excluded(tmp_path):
    path=tmp_path/'fork.jsonl'
    head=meta('child');head['payload'].update(forked_from_id='parent',timestamp='2026-09-20T00:30:00Z')
    old=message('user','inherited parent secret');old['timestamp']='2026-09-20T00:00:00Z'
    write_jsonl(path,[head,meta('parent'),old,message('assistant','child finding',phase='final_answer')])
    inc=transcript.read_material(path,0,session_id='child')
    assert inc['status']=='ok' and 'child finding' in inc['text']
    assert 'inherited parent secret' not in inc['text']


def test_harness_catalog_is_noise_not_task_material(tmp_path):
    path=tmp_path/'native.jsonl'
    write_jsonl(path,[meta(),message('user','<recommended_plugins>noise catalog</recommended_plugins>'),
                     message('user','actual user task')])
    inc=transcript.read_material(path,0,session_id='task-1')
    assert 'catalog' not in inc['text'] and 'actual user task' in inc['text']
