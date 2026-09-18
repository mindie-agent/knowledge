"""Reproduce whole-corpus lexical scoring cost, excluding disk and vectors.

lexical_search retains the pre-catalog scoring algorithm. Enrichment is cleared
to measure the original workload. These are capacity probes using repeated real
VA reference text, not synthetic relevance labels.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mindie_knowledge.corpus import iter_entry_files
from mindie_knowledge.markdown import load_document
from mindie_knowledge.retrieval import lexical_search


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[65, 1000, 10000])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and repeats must be positive")
    bases = [load_document(path, layer="shared") for path in iter_entry_files()]
    report = {"scope": __doc__.strip(), "platform": platform.platform(), "python": platform.python_version(), "runs": []}
    for size in args.sizes:
        documents = [dataclasses.replace(bases[number % len(bases)], uri=f"viking://resources/shared/baseline/{number}.md", retrieval={}) for number in range(size)]
        times = []
        for _ in range(args.repeats):
            started = time.perf_counter()
            lexical_search("Ascend910B4 matmul torch_npu memory_bandwidth_peak", documents, limit=8)
            times.append((time.perf_counter() - started) * 1000)
        row = {"documents": size, "median_ms": statistics.median(times), "samples_ms": times}
        report["runs"].append(row)
        print(json.dumps(row), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
