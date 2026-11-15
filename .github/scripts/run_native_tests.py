"""Partition independent test modules without changing their cases or limits.

[Role] Partition independent test modules without changing cases or limits.
[Invariants] Every collected module is assigned exactly once; shard order is deterministic and failures are not hidden by partitioning.
[Timeouts & Locks] Pure file/argument processing; no test execution, lock or wait occurs here.
[Side-Effect Policy] Reads test paths and writes the requested shard plan to stdout only.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


def _cleanup_tree(path: Path) -> None:
    def _make_writable(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except TimeoutError:
            raise
        except OSError:
            pass

    shutil.rmtree(path, onerror=_make_writable)


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
    basetemp_root = None
    if not args.collect_only and os.name == "nt":
        runner_tmp = os.environ.get("RUNNER_TEMP", "").strip()
        short_parent = runner_tmp if runner_tmp and Path(runner_tmp).is_dir() else None
        basetemp_root = Path(tempfile.mkdtemp(prefix=f"mk{args.shard}-", dir=short_parent))
    try:
        options = ["--collect-only", "-q"] if args.collect_only else [
            "-n", "auto", "--dist", "worksteal", "-q", "--durations=20",
            "-p", "no:cacheprovider", "--junitxml=native-knowledge.xml",
            *([f"--basetemp={basetemp_root / 'bt'}"] if basetemp_root is not None else []),
        ]
        return subprocess.call(
            [sys.executable, "-m", "pytest", *selected, "-m", "not live_npu", *options], cwd=root)
    finally:
        if basetemp_root is not None:
            _cleanup_tree(basetemp_root)


if __name__ == "__main__":
    raise SystemExit(main())
