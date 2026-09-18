"""Measure real candidate files through capture and query, with counted I/O.

This uses MemoryBackend to isolate local capture/catalog costs. It is not a
native embedding benchmark or a claim about a physical 16 GiB machine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mindie_knowledge.catalog import refresh_catalog
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.server.query import query
from mindie_knowledge.summary_hook import capture_summary

INPUTS = ("mindie_knowledge/catalog.py", "mindie_knowledge/server/capture.py",
          "mindie_knowledge/server/query.py", "mindie_knowledge/summary_hook.py",
          "mindie_knowledge/markdown.py", "tests/performance/benchmark_capture_growth.py")


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in INPUTS}


def measure(count):
    with tempfile.TemporaryDirectory(prefix="vaws-capture-growth-") as temporary:
        root = Path(temporary)
        notes = root / "candidate"
        notes.mkdir()
        for number in range(count):
            (notes / f"old-{number:06d}.md").write_text(
                f"# ACLGraph case {number}\n\nObserved graphquartz{number} during replay; cause uncertain.", encoding="utf-8")
        config = load_config({"backend": "memory", "state_root": str(root / "state"),
                              "layers": {"candidate": str(notes), "shared": {"enabled": False},
                                         "project": {"enabled": False}}}, env={})
        opened = []
        original = Path.open

        def counted(path, *args, **kwargs):
            if path.parent == notes and path.suffix == ".md":
                opened.append(path.name)
            return original(path, *args, **kwargs)

        def summary(number):
            return capture_summary({"hook_event_name": "Stop", "last_assistant_message":
                                    f"Synthetic observation {number}: ACLGraph replay differs; cause uncertain."},
                                   config=config, client="codex")

        with patch.object(Path, "open", counted):
            start = time.perf_counter()
            cold = summary("cold")
            cold_ms = (time.perf_counter() - start) * 1000
        cold_reads = sum(name.startswith("old-") for name in opened)
        assert cold["status"] == "saved" and cold["title_lookup"]["incomplete"]
        assert cold_reads <= 32 and not config.state_root.exists()
        start = time.perf_counter()
        catalog = refresh_catalog(config)
        refresh_ms = (time.perf_counter() - start) * 1000
        assert catalog["status"] == "ready"
        opened.clear()
        capture_ms = []
        with patch.object(Path, "open", counted):
            for number in range(10):
                start = time.perf_counter()
                captured = summary(number)
                capture_ms.append((time.perf_counter() - start) * 1000)
                assert captured["title_lookup"]["method"] == "catalog"
        warm_old_reads = sum(name.startswith("old-") for name in opened)
        assert warm_old_reads == 0
        opened.clear()
        query_ms = []
        with patch.object(Path, "open", counted):
            for _ in range(10):
                start = time.perf_counter()
                found = query(config, text="graphquartz31", limit=1)
                query_ms.append((time.perf_counter() - start) * 1000)
                assert len(found.results) == 1 and found.source_reads == 1 and not found.incomplete
        assert len(opened) == 10
        return {"existing_notes": count, "cold_capture_ms": round(cold_ms, 3),
                "cold_old_body_reads": cold_reads, "catalog_refresh_ms": round(refresh_ms, 3),
                "warm_captures": 10, "warm_capture_old_body_reads": warm_old_reads,
                "warm_capture_p50_ms": round(statistics.median(capture_ms), 3),
                "warm_capture_max_ms": round(max(capture_ms), 3),
                "warm_queries": 10, "warm_query_body_reads": len(opened),
                "warm_query_p50_ms": round(statistics.median(query_ms), 3),
                "warm_query_max_ms": round(max(query_ms), 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    before = hashes()
    rows = [measure(count) for count in (64, 1024)]
    assert hashes() == before, "source changed during acceptance"
    result = {"schema": 1, "backend": "MemoryBackend", "python": platform.python_version(),
              "platform": sys.platform, "io_instrumented": True, "source_sha256": before, "runs": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "runs": rows}))


if __name__ == "__main__":
    main()
