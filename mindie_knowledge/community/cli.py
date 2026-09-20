"""Executable community CLI: submit / reconcile.

Contributor-side publication only. Repository-side review and Skill
consolidation are performed by the EXTERNAL Grok Bot software under the
maintainer's own deployment — see docs/repository-bot-contract.md; this
package intentionally ships no bot runtime, no model bridge and no scheduler.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mindie_knowledge._common import EXIT_OK, EXIT_USAGE, ToolError, configure_utf8_stdio

from .common import CommunityError
from .publish import reconcile_batch, submit_batch
from .settings import load_settings_file


def _load_settings(args) -> dict:
    settings = load_settings_file(Path(args.settings))
    if args.state_dir is None:
        args.state_dir = str(Path(args.settings).resolve().parent / "community-state")
    return settings


def _read_json_arg(value: str):
    if value == "-":
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ToolError("stdin JSON exceeds the 8 MiB limit")
    else:
        path = Path(value)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ToolError(f"{path}: cannot read: {exc.strerror}") from None
        if len(raw) > 8 * 1024 * 1024:
            raise ToolError(f"{path}: exceeds the 8 MiB limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError(f"invalid JSON input: {exc}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mindie_knowledge.community",
        description="MindIE community contribution: publish or reconcile a batch.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def base(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--settings", required=True, help="community config JSON (mindie-community-config/1)")
        p.add_argument("--state-dir", help="private state directory (default: beside the config)")
        return p

    p_submit = base("submit", "publish one contribution batch")
    p_submit.add_argument("--batch", required=True, help="batch JSON file, or - for stdin")
    p_reconcile = base("reconcile", "read-only reconciliation of one batch id")
    p_reconcile.add_argument("--batch-id", required=True)

    args = parser.parse_args(argv)
    try:
        settings = _load_settings(args)
        state_dir = Path(args.state_dir)
        if args.command == "submit":
            result = submit_batch(_read_json_arg(args.batch), settings, state_dir)
        elif args.command == "reconcile":
            result = reconcile_batch(args.batch_id, settings, state_dir)
        else:  # pragma: no cover - argparse enforces choices
            parser.error("unknown command")
            return EXIT_USAGE
    except (CommunityError, ToolError) as exc:
        print(json.dumps({"status": getattr(exc, "status", "failed"), "detail": str(exc)[:800]},
                         ensure_ascii=False))
        return EXIT_USAGE
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
