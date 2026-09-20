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
