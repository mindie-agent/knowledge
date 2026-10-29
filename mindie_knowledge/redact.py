#!/usr/bin/env python3
"""[Role] Tree/scalar redaction scanner and CLI over the versioned ruleset.

[Invariants]
- Scans every mapping key and string value; findings are reported masked by
  default, and `--check` exits non-zero when any finding exists.
- Runtime allowlists are local to one invocation; they never alter the built-in
  profile that the main repository uses for re-scans.
- Output rewriting replaces only claimed spans and never treats JSON escapes as
  source text (`scan_tree` / `redact_tree`).

[Timeouts & Locks]
- Pure in-process scanning with no subprocess or network wait; file reads are
  bounded by the caller's corpus iterator.

[Side-Effect Policy]
- Returns findings or rewritten values; writes only the requested CLI stdout/
  report text. Never mutates source documents in place.
"""
from __future__ import annotations

import heapq
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from mindie_knowledge._common import (
    EXIT_FINDINGS,
    EXIT_OK,
    ToolError,
    iter_corpus_files,
    json_pointer,
    load_document,
    relpath,
    run_cli,
)
from mindie_knowledge.redact_rules import (
    BUILTIN_ALLOW_PATTERNS,
    BUILTIN_ALLOWLIST,
    REDACTION_PROFILE,
    RULE_IDS,
    RULES,
    Rule,
)


class Allowlist:
    """Combined built-in + run-time allowlist."""

    def __init__(self, terms: Iterable[str] = (), patterns: Iterable[str] = ()):
        self.terms: set[str] = set(BUILTIN_ALLOWLIST)
        self.patterns: list["re.Pattern[str]"] = [
            re.compile(p, re.IGNORECASE) for p in BUILTIN_ALLOW_PATTERNS
        ]
        for term in terms:
            self.add(term)
        for pat in patterns:
            self.add("re:" + pat)

    def add(self, term: str) -> None:
        term = term.strip()
        if not term:
            return
        if term.startswith("re:"):
            try:
                self.patterns.append(re.compile(term[3:], re.IGNORECASE))
            except re.error as exc:
                raise ToolError(f"allowlist: invalid regular expression {term[3:]!r}: {exc}") from None
        else:
            self.terms.add(term.lower())

    @classmethod
    def from_args(cls, terms: Sequence[str] = (), files: Sequence[str] = ()) -> "Allowlist":
        allow = cls(terms)
        for raw in files:
            doc = load_document(Path(raw))
            if not isinstance(doc, list) or not all(isinstance(x, str) for x in doc):
                raise ToolError(
                    f"{raw}: allow-file must be a YAML/JSON list of strings "
                    "(prefix a regular expression with 're:')"
                )
            for item in doc:
                allow.add(item)
        return allow

    def is_allowed(self, value: str) -> bool:
        v = value.strip()
        if v.lower() in self.terms:
            return True
        return any(p.fullmatch(v) for p in self.patterns)


@dataclass
class Finding:
    path: str
    rule: str
    value: str
    hint: str
    file: str | None = None
    start: int | None = None
    end: int | None = None

    def masked(self) -> str:
        v = self.value
        if len(v) <= 4:
            body = "…"
        else:
            body = f"{v[:2]}…{v[-2:]}"
        return f"{body} ({len(v)} chars)"

    def render(self, show_matches: bool = False) -> str:
        where = f"{self.file}: {self.path}" if self.file else self.path
        shown = self.value if show_matches else self.masked()
        return f"{where}: [{self.rule}] {shown} — {self.hint}"


def scan_text(text: str, allow: Allowlist | None = None, path: str = "<text>",
              *, include_overlaps: bool = False) -> list[Finding]:
    """Report disjoint findings, or every claim for source-side union redaction."""
    allow = allow or Allowlist()
    findings: list[Finding] = []
    claimed: list[tuple[int, int]] = []
    placeholders = [m.span() for m in re.finditer(
        r"<redacted:[a-z0-9-]+:[0-9a-f]{12}>", text)]
    for rule in RULES:
        # Each built-in finder yields disjoint spans in source order. Merge
        # its claims once, instead of comparing every match to every earlier
        # match (quadratic for a long public conversation with many hosts).
        added = []
        cursor = 0
        placeholder_cursor = 0
        for start, end in rule.spans(text):
            if start == end:
                continue
            while (placeholder_cursor < len(placeholders)
                   and placeholders[placeholder_cursor][1] <= start):
                placeholder_cursor += 1
            if (placeholder_cursor < len(placeholders)
                    and placeholders[placeholder_cursor][0] <= start
                    and end <= placeholders[placeholder_cursor][1]):
                continue
            while cursor < len(claimed) and claimed[cursor][1] <= start:
                cursor += 1
            if not include_overlaps and cursor < len(claimed) and claimed[cursor][0] < end:
                continue
            value = text[start:end]
            if allow.is_allowed(value):
                added.append((start, end))
                continue
            added.append((start, end))
            findings.append(
                Finding(
                    path=path,
                    rule=rule.id,
                    value=value,
                    hint=rule.hint,
                    start=start,
                    end=end,
                )
            )
        claimed = list(heapq.merge(claimed, added))
    return findings


def scan_tree(tree: Any, allow: Allowlist | None = None, path: tuple[Any, ...] = ()) -> list[Finding]:
    """Scan every string in a parsed YAML/JSON tree, keys included."""
    allow = allow or Allowlist()
    out: list[Finding] = []
    if isinstance(tree, Mapping):
        for key, value in tree.items():
            if isinstance(key, str):
                out.extend(scan_text(key, allow, json_pointer(path + (f"<key {key!r}>",))))
            out.extend(scan_tree(value, allow, path + (key,)))
    elif isinstance(tree, (list, tuple)):
        for index, value in enumerate(tree):
            out.extend(scan_tree(value, allow, path + (index,)))
    elif isinstance(tree, str):
        out.extend(scan_text(tree, allow, json_pointer(path)))
    return out


def scan_file(path: Path, allow: Allowlist | None = None) -> list[Finding]:
    if path.suffix.lower() in {".md", ".markdown"}:
        try:
            text = path.read_text(encoding="utf-8")
        except TimeoutError:
            raise
        except OSError as exc:
            raise ToolError(f"{path}: cannot read file: {exc}") from exc
        findings = scan_text(text, allow)
    else:
        findings = scan_tree(load_document(path), allow)
    label = relpath(path)
    for f in findings:
        f.file = label
    return findings


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
    parser.add_argument("paths", nargs="*", help="YAML/JSON files or directories")
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
        return EXIT_FINDINGS
    return EXIT_OK



if __name__ == "__main__":
    run_cli(main)
