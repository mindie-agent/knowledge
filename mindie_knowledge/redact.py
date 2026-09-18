"""Compatibility CLI over the shared, zero-dependency redaction rules.

YAML loading and CLI exit codes remain owned by mindie-knowledge.
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Sequence
from mindie_diagnostics import redact as _shared
from mindie_diagnostics.redact import (Rule, Finding, RULES, RULE_IDS, REDACTION_PROFILE,
    BUILTIN_ALLOWLIST, BUILTIN_ALLOW_PATTERNS, scan_text, scan_tree, redact_text)
from mindie_knowledge._common import (EXIT_FINDINGS, EXIT_OK, ToolError,
    iter_corpus_files, load_document, relpath, run_cli)

class Allowlist(_shared.Allowlist):
    def add(self, term: str) -> None:
        try:
            super().add(term)
        except _shared.ToolError as exc:
            from mindie_knowledge.observability import cli_outcome
            cli_outcome("argument_validation", 2, caller=True, exception=exc)
            raise ToolError(str(exc)) from None

    @classmethod
    def from_args(cls, terms: Sequence[str] = (), files: Sequence[str] = ()):
        allow = cls(terms)
        for raw in files:
            doc = load_document(Path(raw))
            if not isinstance(doc, list) or not all(isinstance(x, str) for x in doc):
                raise ToolError(f"{raw}: allow-file must be a YAML/JSON list of strings (prefix a regular expression with 're:')")
            for item in doc:
                allow.add(item)
        return allow

def scan_file(path: Path, allow: Allowlist | None = None) -> list[Finding]:
    if path.suffix.lower() in {".md", ".markdown"}:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"{path}: cannot read file: {exc}") from exc
        findings = scan_text(text, allow)
    else:
        findings = scan_tree(load_document(path), allow)
    label = relpath(path)
    for f in findings:
        f.file = label
    return findings


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str]) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="redact.py",
        description=(
            f"Scan knowledge documents for values that must not be published "
            f"(ruleset {REDACTION_PROFILE})."
        ),
        epilog=(
            "Rules: " + ", ".join(RULE_IDS) + ". Allowlist entries are exact, "
            "case-insensitive values; prefix with 're:' for a full-match regex."
        ),
    )
    parser.add_argument("paths", nargs="*", help="Markdown files or directories (YAML/JSON configuration also accepted)")
    parser.add_argument("--check", action="store_true", help="exit 1 when any finding is reported")
    parser.add_argument("--show-matches", action="store_true", help="print raw matched values (local use only)")
    parser.add_argument("--allow", action="append", default=[], metavar="TERM", help="allowlist a value or 're:<regex>'")
    parser.add_argument("--allow-file", action="append", default=[], metavar="FILE", help="YAML/JSON list of allowlist terms")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--profile", action="store_true", help="print the ruleset version and exit")
    args = parser.parse_args(argv)

    if args.profile:
        print(REDACTION_PROFILE)
        return EXIT_OK
    if not args.paths:
        parser.error("at least one path is required (or --profile)")

    allow = Allowlist.from_args(args.allow, args.allow_file)
    findings: list[Finding] = []
    files = 0
    errors = 0
    for path in iter_corpus_files(args.paths):
        files += 1
        try:
            findings.extend(scan_file(path, allow))
        except ToolError as exc:
            errors += 1
            print(str(exc), file=sys.stderr)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "redaction_profile": REDACTION_PROFILE,
                    "files": files,
                    "findings": [
                        {
                            "file": f.file,
                            "path": f.path,
                            "rule": f.rule,
                            "value": f.value if args.show_matches else f.masked(),
                            "hint": f.hint,
                        }
                        for f in findings
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for f in findings:
            print(f.render(args.show_matches))
        print(
            f"redact {REDACTION_PROFILE}: {files} file(s), {len(findings)} finding(s)"
            + (f", {errors} unreadable" if errors else ""),
            file=sys.stderr,
        )

    if errors:
        return EXIT_FINDINGS
    if findings and args.check:
        from mindie_knowledge.observability import cli_outcome
        cli_outcome("redaction.findings", EXIT_FINDINGS, count=len(findings))
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
