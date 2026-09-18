"""Prepare a public Markdown copy or submit it for human review."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from mindie_knowledge.contribution.github import UrllibContributionGitHub
from mindie_knowledge.contribution.submit import SubmitConfig, prepare_candidate, submit_pending


def _print(payload: dict) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0

def _cmd_prepare(args: argparse.Namespace) -> int:
    record = prepare_candidate(
        Path(args.candidate),
        state_root=Path(args.state_root),
        public_root=Path(args.public_root),
    )
    return _print(record.to_dict())

def _cmd_submit(args: argparse.Namespace) -> int:
    from mindie_knowledge.contribution.pending import iter_pending

    records = [record for record in iter_pending(Path(args.state_root))
               if record.status in {"pending", "awaiting_transport"}]
    if not records:
        return _print({"status": "unchanged", "pending": []})
    if not args.git_repo:
        return _print({"status": "awaiting_transport", "reason": "a configured fork checkout is required"})
    from mindie_knowledge.github_transport import github_token, git_environment

    try:
        token = github_token()
    except (OSError, RuntimeError) as exc:
        return _print({"status": "awaiting_transport", "reason": str(exc)})
    github = UrllibContributionGitHub(token)
    config = SubmitConfig(upstream=args.upstream, fork=args.fork or args.upstream, push_remote="origin")
    updated = submit_pending(
        records[0],
        state_root=Path(args.state_root),
        public_root=Path(args.public_root),
        git_repo=Path(args.git_repo),
        github=github,
        config=config,
        git_env=git_environment(token),
    )
    return _print(updated.to_dict())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mindie_knowledge.contribution")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="redact a public copy and write a pending record")
    prepare.add_argument("--candidate", required=True)
    prepare.add_argument("--state-root", required=True)
    prepare.add_argument("--public-root", required=True)
    prepare.set_defaults(func=_cmd_prepare)

    submit = sub.add_parser("submit", help="submit pending public copies (needs transport)")
    submit.add_argument("--state-root", required=True)
    submit.add_argument("--public-root", required=True)
    submit.add_argument("--git-repo")
    submit.add_argument("--upstream", default="owner/mindie-knowledge-corpus")
    submit.add_argument("--fork")
    submit.set_defaults(func=_cmd_submit)

    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
