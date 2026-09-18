"""Save the final text provided by a native hook. Never read full transcripts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any

from mindie_knowledge.distribution.errors import SwitchInProgress
from mindie_knowledge.distribution.sync import SwitchLock
from mindie_knowledge.markdown import document_slug
from mindie_knowledge.server.capture import candidate_root, capture, lookup_capture
from mindie_knowledge.server.layers import ServiceConfig, load_config
from mindie_knowledge.observability import observed, capture_failure


def capture_summary(payload: dict[str, Any], *, config: ServiceConfig, client: str) -> dict[str, Any]:
    # Use each client's documented response event. Grok can import other
    # clients' hooks, so its native marker must not be handled a second time.
    if client == "grok":
        if payload.get("hookEventName") != "stop":
            return {"status": "no_summary"}
        text = payload.get("lastAssistantMessage")
    elif "hookEventName" in payload:
        return {"status": "no_summary"}
    elif client == "cursor" and payload.get("hook_event_name") == "afterAgentResponse":
        text = payload.get("text")
    elif client in {"codex", "claude", "kimi"} and payload.get("hook_event_name") == "Stop":
        text = payload.get("last_assistant_message")
    else:
        # Client adapters may supply their native final response. Storage stays
        # here; locating a particular client's response stays with its adapter.
        return {"status": "no_summary"}
    if not isinstance(text, str) or not text.strip():
        return {"status": "no_summary"}
    text = re.sub(r"<oai-mem-citation>.*?</oai-mem-citation>", "", text, flags=re.S).strip()
    if len(text) < 24:
        return {"status": "no_summary"}
    # The digest makes repeated hook delivery idempotent without inventing a
    # task identity. Client-provided source fields stay in the private sidecar.
    heading = next((line.strip().lstrip("# ") for line in text.splitlines() if line.strip()), "Session observation")
    title = f"{heading[:100]} [{hashlib.sha256(text.encode()).hexdigest()[:8]}]"
    source = {"client": client}
    for key in ("session_id", "turn_id", "conversation_id", "generation_id", "sessionId", "promptId"):
        if isinstance(payload.get(key), str):
            source[key] = payload[key]
    root = candidate_root(config)
    ident = document_slug(title)
    # Lock only this content-derived identity. Other summaries need not wait,
    # and an overlapping delivery never blocks the native hook. These local
    # lock files are not knowledge entries and do not activate the backend.
    lock = SwitchLock(root / ".summary-locks" / f"{ident}.lock")
    try:
        lock.acquire()
    except SwitchInProgress:
        return {"status": "busy"}
    try:
        lookup = lookup_capture(root, title, config=config, preserve_identity=True)
        existing = lookup.document
        if existing is not None:
            # Preserve the first capture's provenance and timestamp even if
            # another client supplies identical prose. An edited candidate
            # remains the maintainer's version; replay never rolls it back.
            status = "unchanged" if existing.content == text else "preserved"
            return {"status": status, "ref": existing.uri,
                    "contribution": {"status": "unchanged"}}
        if lookup.missing_ref:
            # An observed identity disappearing is consistent with a human
            # rename/delete. Replaying a summary must not undo that decision.
            return {"status": "preserved", "ref": lookup.missing_ref, "title_lookup": lookup.describe()}
        saved = capture(title=title, content=text, source=source, config=config, index=False, _lookup=lookup)
        return {"status": "saved", "ref": saved["ref"], "contribution": saved["contribution"],
                "title_lookup": saved["title_lookup"]}
    finally:
        lock.release()


@observed("knowledge.summary")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read(1_048_577)
        if len(raw) <= 1_048_576:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                capture_summary(payload, config=load_config(path=args.config), client=args.client)
    except Exception as exc:
        capture_failure(exc, "summary_unavailable")  # optional capture never interrupts the client
    print("{}")  # observe-only, never continue or block the client
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
