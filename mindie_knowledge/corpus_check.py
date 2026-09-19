"""Validate public Markdown without executing any corpus checkout code."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mindie_knowledge.redact import scan_text


def validate_corpus(repo: Path) -> dict:
    root = Path(repo) / "corpus"
    files = sorted(root.rglob("*")) if root.is_dir() else []
    problems: list[dict[str, str]] = []
    count = 0
    for path in files:
        relative = path.relative_to(repo).as_posix()
        if path.is_symlink():
            problems.append({"path": relative, "reason": "symlinks are not public knowledge documents"})
            continue
        if path.is_dir():
            continue
        if path.suffix != ".md" or path.stat().st_size > 1_048_576:
            problems.append({"path": relative, "reason": "expected a Markdown document of at most 1 MiB"})
            continue
        try:
            text = path.read_text(encoding="utf-8")
            if not text.strip() or not text.lstrip().startswith("#"):
                raise ValueError("expected a Markdown document with a heading")
            # Report rule IDs, never matched private values in public CI logs.
            rules = sorted({finding.rule for finding in scan_text(text)})
            if rules:
                problems.append({"path": relative, "reason": "redaction required: " + ", ".join(rules)})
            count += 1
        except (OSError, ValueError) as exc:
            problems.append({"path": relative, "reason": type(exc).__name__})
    if not count:
        problems.append({"path": "corpus", "reason": "no knowledge documents"})
    return {"ok": not problems, "documents": count, "problems": problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = validate_corpus(args.repo)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
