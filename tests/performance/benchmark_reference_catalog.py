"""Measure real source reads, SQLite retrieval, query and maintenance phases.

Synthetic scale repeats the checked-in VA corpus, adding unique note markers.
This measures capacity, not relevance. Query embedding/native vector latency
must be measured separately: the public-query phase explicitly disables vectors.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mindie_knowledge.catalog import catalog_path, refresh_catalog, search_catalog
from mindie_knowledge.corpus import iter_entry_files
from mindie_knowledge.evaluation import percentile
from mindie_knowledge.local.backend import MemoryBackend, UnavailableBackend
from mindie_knowledge.maintenance import maintain
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.server.query import query


def peak_memory_bytes():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                                                    "PagefileUsage", "PeakPagefileUsage")]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize
        return None
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def timed(call):
    start = time.perf_counter()
    result = call()
    return result, (time.perf_counter() - start) * 1000


def brief(report):
    return {key: value for key, value in report.items() if key not in {"changed_uris", "deleted_uris", "errors"}}


def run_scale(count: int, *, queries: int, maintenance: bool = True) -> dict:
    bases = [path.read_text(encoding="utf-8") for path in iter_entry_files()]
    if not bases:
        raise RuntimeError("checked-in reference corpus is missing")
    with tempfile.TemporaryDirectory(prefix="vaws-catalog-scale-") as temporary:
        root = Path(temporary)
        notes = root / "notes"
        notes.mkdir()
        source_bytes = 0
        for number in range(count):
            raw = bases[number % len(bases)] + f"\nScale marker scale_case_{number}.\n"
            path = notes / f"case-{number:06d}.md"
            path.write_text(raw, encoding="utf-8")
            source_bytes += len(raw.encode("utf-8"))
        config = load_config({"backend": "unavailable", "state_root": str(root / "state"),
                              "shared_sync": {"enabled": False}, "publishing": {"enabled": False},
                              "layers": {"shared": {"enabled": False}, "candidate": {"enabled": False}, "project": str(notes)}}, env={})
        cold, cold_ms = timed(lambda: query(config, text="Ascend910B4 matmul"))
        built, build_ms = timed(lambda: refresh_catalog(config))
        if built["status"] != "ready":
            raise RuntimeError(f"catalog build failed: {built}")
        unchanged, unchanged_ms = timed(lambda: refresh_catalog(config))
        first = notes / "case-000000.md"
        first.write_text(first.read_text(encoding="utf-8") + "\nA changed observed condition.\n", encoding="utf-8")
        changed, changed_ms = timed(lambda: refresh_catalog(config))
        if unchanged["parsed"] or unchanged["read_bytes"] or changed["parsed"] != 1:
            raise AssertionError("incremental reuse contract failed")
        questions = ["Ascend910B4 matmul torch_npu memory_bandwidth_peak", "CANN platform_config fp16_dense_matmul_peak",
                     f"scale_case_{count // 2}", "Ascend910B4 theoretical measured"]
        for question in questions:
            search_catalog(config, question, limit=32)
            query(config, text=question)
        lexical, public, output_sizes, reads = [], [], [], []
        for number in range(queries):
            question = questions[number % len(questions)]
            _, duration = timed(lambda: search_catalog(config, question, limit=32))
            lexical.append(duration)
            response, duration = timed(lambda: query(config, text=question))
            public.append(duration)
            output_sizes.append(len(json.dumps(response.to_dict(), ensure_ascii=False).encode("utf-8")))
            reads.append(response.source_reads)
        result = {"documents": count, "source_bytes": source_bytes, "synthetic_repeated_real_corpus": True,
                  "cold_query_ms": cold_ms, "cold_query_incomplete": cold.incomplete,
                  "build_ms": build_ms, "unchanged_refresh_ms": unchanged_ms, "changed_refresh_ms": changed_ms,
                  "build": brief(built), "unchanged_refresh": brief(unchanged), "changed_refresh": brief(changed),
                  "catalog_search_ms": {"p50": percentile(lexical, .5), "p95": percentile(lexical, .95), "max": max(lexical)},
                  "public_query_vectors_disabled_ms": {"p50": percentile(public, .5), "p95": percentile(public, .95), "max": max(public)},
                  "query_max_source_reads": max(reads), "query_max_output_bytes": max(output_sizes),
                  "catalog_disk_bytes": catalog_path(config).stat().st_size, "peak_rss_bytes_before_vector_fixture": peak_memory_bytes()}
        if maintenance:
            config.retrieval = MemoryBackend(config)
            initial, initial_ms = timed(lambda: maintain(config, verify=True, force=True))
            warm, warm_ms = timed(lambda: maintain(config, force=True))
            result["maintenance_memory_backend"] = {"initial_ms": initial_ms, "unchanged_ms": warm_ms,
                                                     "initial_ready": initial.get("ready"), "unchanged_ready": warm.get("ready"),
                                                     "scope": "Actual maintenance control plane with a memory vector fixture; excludes native embedding cost."}
            del config.retrieval
            gc.collect()
        result["peak_rss_bytes"] = peak_memory_bytes()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 100000])
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-maintenance", action="store_true")
    args = parser.parse_args()
    if any(size < 1 for size in args.sizes) or args.queries < 1:
        parser.error("sizes and queries must be positive")
    report = {"platform": platform.platform(), "python": platform.python_version(), "logical_cpus": os.cpu_count(),
              "scope": "CPU catalog and public lexical query on real files; synthetic capacity data, not a quality or native-vector benchmark.",
              "runs": []}
    for size in args.sizes:
        result = run_scale(size, queries=args.queries, maintenance=not args.skip_maintenance)
        report["runs"].append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
