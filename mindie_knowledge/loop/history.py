"""Explicit local historical planning. No model, service, grant or publication.

The operator must name one source and its native task id. The output consists
of bounded public-material packets and a durable byte cursor; it is NOT an
experience corpus. It may contain private task data and must stay local.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .transcript import FileIdentity, identify, read_material


def plan(source, session_id, output, *, max_bytes=64*1024*1024, max_seconds=15):
    source = Path(source).resolve()
    output = Path(output)
    if not 1 <= max_bytes <= 1024*1024*1024 or not 0 < max_seconds <= 60:
        raise ValueError("history plan exceeds invocation budget")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = output / "plan.json"
    if manifest.exists():
        state = json.loads(manifest.read_text())
        if state["source"] != str(source) or state["session_id"] != session_id:
            raise ValueError("plan belongs to another source or task")
        expected = FileIdentity.unserialize(state["identity"], str(source))
        if expected is None:
            raise ValueError("invalid saved identity")
    else:
        expected = identify(source)
        if expected is None:
            raise ValueError("source unavailable")
        state = dict(schema="mindie-history-plan/1",source=str(source),session_id=session_id,
                     identity=expected.serialize(),snapshot_size=expected.size,
                     cursor=0,packets=0,public_records=0,skipped_records=0,
                     oversize_pieces=0,coverage_gaps=0,public_bytes=0,status="pending")
    start = state["cursor"]
    begun = time.monotonic()
    while state["cursor"] < state["snapshot_size"]:
        left = max_bytes-(state["cursor"]-start)
        seconds = max_seconds-(time.monotonic()-begun)
        if left < 1024 or seconds <= 0:
            break
        inc = read_material(source,state["cursor"],session_id=session_id,not_before=None,
                            expected=expected,max_scan_bytes=min(left,16*1024*1024),
                            max_seconds=min(seconds,2))
        if inc["status"] not in {"ok","unchanged"}:
            state.update(status=inc["status"],detail=inc.get("coverage_note"))
            break
        if inc["end"] <= state["cursor"]:
            state.update(status="partial-tail" if inc["partial"] else "stalled")
            break
        # Freeze a history snapshot; an active source can append but must not
        # silently make this historical plan an ongoing capture mechanism.
        if inc["end"] > state["snapshot_size"]:
            state.update(status="source-grew-at-snapshot-boundary")
            break
        packet = output / f"{state['packets']:06d}.json"
        packet.write_text(json.dumps(inc,ensure_ascii=False,indent=2))
        packet.chmod(0o600)
        state["cursor"] = inc["end"]
        state["packets"] += 1
        state["public_records"] += inc["records"]
        state["skipped_records"] += inc["skipped_records"]
        state["oversize_pieces"] += inc["oversize_records"]
        state["coverage_gaps"] += len(inc["coverage"])
        state["public_bytes"] += len(inc["text"].encode())
        state["status"] = "complete" if state["cursor"] == state["snapshot_size"] else "pending"
        temp = output / "plan.next"
        temp.write_text(json.dumps(state,indent=2));temp.chmod(0o600);temp.replace(manifest)
    state["last_scan_bytes"] = state["cursor"]-start
    state["last_seconds"] = round(time.monotonic()-begun,3)
    temp = output / "plan.next"
    temp.write_text(json.dumps(state,indent=2));temp.chmod(0o600);temp.replace(manifest)
    return state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    cmd=sub.add_parser("plan")
    cmd.add_argument("--source",required=True)
    cmd.add_argument("--session-id",required=True)
    cmd.add_argument("--output",required=True)
    cmd.add_argument("--max-bytes",type=int,default=64*1024*1024)
    cmd.add_argument("--max-seconds",type=float,default=15)
    args=parser.parse_args()
    print(json.dumps(plan(args.source,args.session_id,args.output,
                         max_bytes=args.max_bytes,max_seconds=args.max_seconds)))


if __name__ == "__main__":
    main()
