"""CLI for Markdown capture and delete."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mindie_knowledge.server.capture import capture, delete
from mindie_knowledge.server.layers import load_config


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Capture or delete a local candidate")
    parser.add_argument("--title", default="")
    parser.add_argument("--content", default="")
    parser.add_argument("--content-file", default="")
    parser.add_argument("--delete", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--backend", default="")
    args = parser.parse_args(argv)
    mapping = {"backend": args.backend} if args.backend else None
    config = load_config(mapping, path=args.config or None)
    if args.delete:
        print(json.dumps(delete(args.delete, config=config), ensure_ascii=False, indent=2))
        return 0
    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    payload = capture(title=args.title, content=content, config=config, index=False)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
