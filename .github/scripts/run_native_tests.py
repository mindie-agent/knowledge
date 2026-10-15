"""Partition independent test modules without changing their cases or limits."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    if args.shards < 1 or not 0 <= args.shard < args.shards:
        parser.error("shard must be in [0, shards), with positive shards")

    root = Path(__file__).resolve().parents[2]
    files = sorted(
        path.relative_to(root).as_posix()
        for path in (root / "tests").rglob("*.py")
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    )
    selected = files[args.shard::args.shards]
    if not selected:
        parser.error("test shard is empty")
    manifest = dict(shard=args.shard, shards=args.shards,
                    all_files=files, selected_files=selected)
    (root / "native-knowledge-files.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Native tests: shard {args.shard + 1}/{args.shards}, "
          f"{len(selected)}/{len(files)} modules", flush=True)
    options = ["--collect-only", "-q"] if args.collect_only else [
        "-v", "--maxfail=1", "--durations=25", "--junitxml=native-knowledge.xml",
    ]
    return subprocess.call([sys.executable, "-m", "pytest", *selected, *options], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
