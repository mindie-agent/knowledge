"""Console entry for ``mindie-knowledge`` / ``python -m mindie_knowledge``."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable
from mindie_knowledge.observability import observed


def _dispatch(handler: Callable[[list[str]], int], argv: list[str]) -> int:
    from mindie_knowledge._common import ToolError, EXIT_USAGE

    try:
        return handler(argv)
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return 130


@observed("knowledge.cli")
def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="mindie-knowledge",
        description="Engine CLI for the mindie-knowledge commons. The corpus ships in the wheel.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "server",
            "diagnostics",
            "prepare",
            "health",
            "catalog",
            "evaluate",
            "code-map",
            "relations",
            "curation",
            "curation-export",
            "redact",
            "query",
            "capture",
            "contribution",
            "distribution",
            "publishing",
            "skill",
        ),
        help="subcommand",
    )
    parser.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="arguments forwarded to the subcommand",
    )
    if not argv or argv[0] in ("-h", "--help"):
        parser.print_help()
        return 0
    if argv[0] == "help":
        parser.print_help()
        return 0

    args = parser.parse_args(argv)
    command = args.command
    rest = list(args.rest)
    if rest[:1] == ["--"]:
        rest = rest[1:]

    if command == "diagnostics":
        from mindie_diagnostics.cli import main as diagnostics_main
        return diagnostics_main(rest)

    if command == "server":
        from mindie_knowledge.server.mcp_server import main as server_main

        return server_main(rest)
    if command == "prepare":
        from mindie_knowledge.maintenance import main as prepare_main

        return prepare_main(rest)
    if command == "curation":
        from mindie_knowledge.curation import main as curation_main

        return curation_main(rest)
    if command == "curation-export":
        from mindie_knowledge.curation_export import main as export_main

        return export_main(rest)
    if command in {"catalog", "evaluate", "code-map", "relations"}:
        from mindie_knowledge.reference_cli import main as reference_main

        return reference_main([command, *rest])
    if command == "health":
        import json
        from mindie_knowledge.health import inspect_knowledge
        from mindie_knowledge.server.layers import load_config

        health_parser = argparse.ArgumentParser(description="Inspect local maintenance hints without models, network or note edits")
        health_parser.add_argument("--config")
        health_parser.add_argument("--limit", type=int, default=50, help="maximum findings to print (default: 50)")
        options = health_parser.parse_args(rest)
        if options.limit < 1:
            health_parser.error("--limit must be positive")
        report = inspect_knowledge(load_config(path=options.config))
        findings = report.get("findings", [])
        report.update(finding_count=len(findings), findings=findings[:options.limit], truncated=len(findings) > options.limit)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if command == "redact":
        from mindie_knowledge.redact import main as redact_main

        return _dispatch(redact_main, rest)
    if command == "query":
        from mindie_knowledge.server.query import main as query_main

        return query_main(rest)
    if command == "capture":
        from mindie_knowledge.server.capture_cli import main as capture_main

        return _dispatch(capture_main, rest)
    if command == "contribution":
        from mindie_knowledge.contribution.__main__ import main as contribution_main

        return contribution_main(rest)
    if command == "distribution":
        from mindie_knowledge.distribution.__main__ import main as distribution_main

        return distribution_main(rest)
    if command == "publishing":
        from mindie_knowledge.publishing import main as publishing_main

        return publishing_main(rest)
    if command == "skill":
        from mindie_knowledge.skill import main as skill_main

        return skill_main(rest)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
