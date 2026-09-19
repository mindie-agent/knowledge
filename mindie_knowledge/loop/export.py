"""Write an inspectable versioned feed from authorized (published) entries.

This is the production writer for the export protocol that the independent
``knowledge_intake.feed_sync.verified_snapshot`` reader verifies; format
constants are imported from that reader so producer and reader cannot drift
apart silently. It writes files only — no Git commands, no network, and it
never pushes an official branch. The pointer swap is the commit point: any
failure before it retains the previous export. Withdrawn entries simply
disappear from the next generation, which the reader turns into deactivation.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path, PurePosixPath

import hashlib

from mindie_knowledge.markdown import _atomic_write_text, render_markdown
from mindie_knowledge.redact import REDACTION_PROFILE, scan_text

from .store import canonical


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    ).encode()


def _filename(title, ident):
    base = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "entry"
    return f"{base[:60]}-{ident[:12]}.md"


def export_feed(store, output):
    """Export the store's currently authorized entries as one new generation.

    Returns the written pointer. An empty authorized set is valid: it produces
    an empty generation that switches downstream feeds to no active entries,
    which is how withdrawing the final entry propagates. Raises without
    touching the previous export when any entry fails validation.

    Alongside the verified Markdown/manifest protocol, one authenticated
    ``mindie-loop.json`` extension carries each entry's canonical identity
    (id, source, conditions, producers) and the minimal effective feedback
    votes, so a Git round-trip preserves Store identity and producer
    independence instead of making every publisher an unrelated feed producer.
    Raw captures, use evidence and judge prose are never exported.
    """
    from knowledge_intake.feed_sync import PROFILE, SCHEMA

    if PROFILE != REDACTION_PROFILE:
        raise ValueError(
            f"feed reader expects redaction profile {PROFILE}, local ruleset is {REDACTION_PROFILE}"
        )
    output = Path(output)
    snapshot = store.snapshot()

    rows, files = [], {}
    loop_entries = []
    for doc in sorted(snapshot["entries"], key=lambda d: d["id"]):
        subdir = "topics" if doc["kind"] == "knowledge" else "cases"
        body = render_markdown(doc["title"], doc["content"])
        findings = (
            scan_text(body)
            + scan_text(canonical(doc["source"]))
            + scan_text(canonical(doc["conditions"]))
        )
        if findings:
            rules = ", ".join(sorted({finding.rule for finding in findings}))
            raise ValueError(
                f"authorized entry {doc['id']} no longer passes redaction: {rules}"
            )
        if any(not isinstance(v, str) for v in doc["conditions"].values()):
            raise ValueError(f"entry {doc['id']} has non-text applicability values")
        raw = body.encode("utf-8")
        normalized = _hash(
            body.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        )
        metadata_raw = _encoded(
            {
                "conditions": doc["conditions"],
                "retrieval": {
                    "source_sha256": normalized,
                    "aliases": [],
                    "topics": [],
                },
            }
        )
        name = f"{subdir}/{_filename(doc['title'], doc['id'])}"
        files[name] = raw
        files[str(PurePosixPath(name).with_suffix(".meta.json"))] = metadata_raw
        rows.append(
            {
                "path": name,
                "size": len(raw),
                "sha256": _hash(raw),
                "source_sha256": normalized,
                "input_sha256": _hash(raw),
                "metadata_size": len(metadata_raw),
                "metadata_sha256": _hash(metadata_raw),
            }
        )
        loop_entries.append(
            {
                "id": doc["id"],
                "kind": doc["kind"],
                "title": doc["title"],
                "content": doc["content"],
                "source": doc["source"],
                "conditions": doc["conditions"],
                "producers": doc["producers"],
                "path": name,
            }
        )

    # The reader protocol requires 1..16 supported roots even when empty.
    includes = sorted({row["path"].split("/", 1)[0] for row in rows}) or [
        "cases",
        "topics",
    ]
    extension_raw = _encoded(
        {
            "schema": "mindie-loop-export/1",
            "entries": loop_entries,
            "feedback": snapshot["feedback"],
        }
    )
    findings = scan_text(extension_raw.decode("utf-8"))
    if findings:
        rules = ", ".join(sorted({finding.rule for finding in findings}))
        raise ValueError(f"feedback identities failed redaction: {rules}")
    previous_snapshot, old_rows = None, []
    pointer_path = output / "current.json"
    if pointer_path.is_file():
        try:
            pointer = json.loads(pointer_path.read_text())
            old_manifest = json.loads(
                (output / "generations" / pointer["generation"] / "prepared.json").read_text()
            )
            previous_snapshot = pointer["snapshot"]
            old_rows = old_manifest["files"]
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError(
                f"existing export is unreadable; refusing to replace it: {exc}"
            ) from exc
    old_by_path = {row["path"]: row for row in old_rows}
    new_by_path = {row["path"]: row for row in rows}
    added = sorted(set(new_by_path) - set(old_by_path))
    removed = sorted(set(old_by_path) - set(new_by_path))
    updated = sorted(
        name
        for name in set(new_by_path) & set(old_by_path)
        if new_by_path[name]["sha256"] != old_by_path[name]["sha256"]
        or new_by_path[name]["metadata_sha256"] != old_by_path[name]["metadata_sha256"]
    )
    renamed = []
    removed_by_input = {old_rows_by["input_sha256"]: name for name in removed for old_rows_by in [old_by_path[name]]}
    for name in list(added):
        basis = removed_by_input.get(new_by_path[name]["input_sha256"])
        if basis is not None:
            renamed.append({"from": basis, "to": name, "basis": "identical_input_sha256"})
            added.remove(name)
            removed.remove(basis)

    generation = uuid.uuid4().hex
    signature = _hash(
        _encoded({"files": rows, "includes": includes, "redaction_profile": PROFILE})
    )
    manifest_raw = _encoded(
        {
            "schema": SCHEMA,
            "redaction_profile": PROFILE,
            "includes": includes,
            "snapshot": signature,
            "previous_snapshot": previous_snapshot,
            "files": rows,
            "changes": {
                "added": added,
                "removed": removed,
                "updated": updated,
                "renamed": renamed,
            },
        }
    )
    # Write the complete generation first; the pointer swap is the commit point.
    target = output / "generations" / generation
    for name, raw in sorted(files.items()):
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target / name, raw.decode("utf-8"))
    _atomic_write_text(target / "mindie-loop.json", extension_raw.decode("utf-8"))
    _atomic_write_text(target / "prepared.json", manifest_raw.decode("utf-8"))
    pointer = {
        "schema": SCHEMA,
        "generation": generation,
        "manifest_sha256": _hash(manifest_raw),
        "snapshot": signature,
    }
    _atomic_write_text(pointer_path, json.dumps(pointer, indent=2) + "\n")
    return dict(
        pointer=pointer,
        entries=len(rows),
        changes={"added": added, "removed": removed, "updated": updated, "renamed": renamed},
        note="Files only. Review and publish the Git branch through the normal channel.",
    )
