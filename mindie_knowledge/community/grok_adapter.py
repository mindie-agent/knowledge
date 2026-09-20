"""Executable adapter between the community runner and the installed Grok CLI.

The community runner speaks a narrow contract to the configured argv: one
bounded JSON request on stdin, one JSON object (the review or skill proposal)
on stdout. This adapter bridges that contract to the actually installed Grok
CLI 1.0.30 headless flags (verified against `grok --help`):

    grok --prompt-file <tmp> --output-format json --json-schema <tmp>
         --max-turns 1 --no-subagents --disable-web-search
         --permission-mode plan --deny Bash --deny Write --deny Edit

Single bounded turn, tools/subagents/web disabled, temporary UTF-8 prompt and
schema files (deleted afterwards), owned process tree with deadline via
``common.run_argv``. No global Grok config is read or written by this adapter
beyond what the CLI itself does for auth. Hidden reasoning is never parsed:
only the structured result field is extracted from the JSON envelope.

Usage (this is also the value to configure as bot.grok_argv / bot.skill_grok_argv):

    python -m mindie_knowledge.community.grok_adapter --kind review
    python -m mindie_knowledge.community.grok_adapter --kind skill
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .common import CommunityError, run_argv

MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 300

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "verdict", "reason"],
    "properties": {
        "schema": {"type": "string", "enum": ["mindie-review/1"]},
        "verdict": {
            "type": "string",
            "enum": ["accept", "correct", "add_conditions", "retire", "no_change", "uncertain"],
        },
        "reason": {"type": "string", "maxLength": 2000},
        "edits": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "maxLength": 256},
                    "content": {"type": "string", "maxLength": 131072},
                },
            },
        },
        "conditions": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "key", "value"],
                "properties": {
                    "path": {"type": "string", "maxLength": 256},
                    "key": {"type": "string", "maxLength": 128},
                    "value": {"type": "string", "maxLength": 512},
                },
            },
        },
        "retire": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["path", "reason"],
            "properties": {
                "path": {"type": "string", "maxLength": 256},
                "reason": {"type": "string", "maxLength": 2000},
                "replacement_ref": {"type": "string", "maxLength": 256},
            },
        },
    },
}

SKILL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "slug", "skill_md"],
    "properties": {
        "schema": {"type": "string", "enum": ["mindie-skill/1"]},
        "slug": {"type": "string", "maxLength": 64},
        "skill_md": {"type": "string", "maxLength": 65536},
        "references": {
            "type": "object",
            "additionalProperties": {"type": "string", "maxLength": 65536},
            "maxProperties": 8,
        },
    },
}

SCHEMAS = {"review": REVIEW_SCHEMA, "skill": SKILL_SCHEMA}

#: Envelope keys that may carry the structured result. Anything named like
#: reasoning/thinking is deliberately NOT read.
_RESULT_KEYS = ("structured_output", "structuredOutput", "result", "response",
                "message", "content", "text", "output")


def extract_payload(stdout_text: str) -> dict[str, Any]:
    """Extract the proposal JSON object from the CLI's JSON output envelope."""
    text = stdout_text.strip()
    if not text:
        raise CommunityError("grok adapter: empty CLI output")
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the last NDJSON line that parses as an object.
        envelope = None
        for line in reversed(text.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                envelope = candidate
                break
        if envelope is None:
            raise CommunityError("grok adapter: CLI output is not JSON") from None
    if isinstance(envelope, list):
        for item in reversed(envelope):
            if isinstance(item, dict):
                envelope = item
                break
    if not isinstance(envelope, dict):
        raise CommunityError("grok adapter: CLI output envelope is not an object")
    if "schema" in envelope and str(envelope["schema"]).startswith("mindie-"):
        return envelope  # the envelope already is the proposal
    for key in _RESULT_KEYS:
        value = envelope.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise CommunityError("grok adapter: no structured result field in the CLI envelope")


def build_argv(grok: str, prompt_file: Path, schema_file: Path) -> list[str]:
    """The exact supported headless argv for the installed Grok CLI 1.0.30."""
    return [
        grok,
        "--prompt-file", str(prompt_file),
        "--output-format", "json",
        "--json-schema", str(schema_file),
        "--max-turns", "1",
        "--no-subagents",
        "--disable-web-search",
        "--permission-mode", "plan",
        "--deny", "Bash",
        "--deny", "Write",
        "--deny", "Edit",
        "--deny", "NotebookEdit",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mindie_knowledge.community.grok_adapter")
    parser.add_argument("--kind", choices=sorted(SCHEMAS), required=True)
    parser.add_argument("--grok", default=os.environ.get("MINDIE_GROK_BIN", "grok"))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        print("grok adapter: input exceeds the bounded size", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"grok adapter: stdin must be one JSON object: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("grok adapter: stdin must be one JSON object", file=sys.stderr)
        return 2

    prompt = (
        "You are the MindIE community review/consolidation component. Answer with ONE "
        "JSON object matching the provided JSON schema. No prose, no markdown fences.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    prompt_file = schema_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".prompt.md", delete=False
        ) as handle:
            handle.write(prompt)
            prompt_file = Path(handle.name)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".schema.json", delete=False
        ) as handle:
            json.dump(SCHEMAS[args.kind], handle)
            schema_file = Path(handle.name)
        result = run_argv(
            build_argv(args.grok, prompt_file, schema_file),
            timeout=max(30, min(args.timeout, 1800)),
            max_output=MAX_OUTPUT_BYTES,
        )
    finally:
        for tmp in (prompt_file, schema_file):
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    if result.timed_out:
        print("grok adapter: CLI exceeded the deadline", file=sys.stderr)
        return 2
    if result.code != 0:
        detail = result.err_text.strip().splitlines()
        print(f"grok adapter: CLI exited {result.code}: {detail[0][:200] if detail else ''}",
              file=sys.stderr)
        return 2
    try:
        proposal = extract_payload(result.out_text)
    except CommunityError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    json.dump(proposal, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
