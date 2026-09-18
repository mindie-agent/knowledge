"""Explicit shared-source scale acceptance; no network/model/native server.

Creates actual pack/reference assets, an immutable prepared mount and a real
SQLite catalog. Source notes are synthetic repetitions of a VA-domain case;
vector bytes are fixture data (real native import is tested separately).
"""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from distribution.helpers import make_pack, make_manifest, GIT_SHA
from mindie_knowledge.catalog import refresh_catalog
from mindie_knowledge.distribution.manifest import ExpectedContract, atomic_write_json, validate_release_manifest
from mindie_knowledge.distribution.references import prepare_reference, prepare_mount, prepared_shared_documents, write_references
from mindie_knowledge.distribution.sync import CURRENT_SCHEMA, DistributionState
from mindie_knowledge.local.backend import MemoryBackend
from mindie_knowledge.markdown import normalized_sha256
from mindie_knowledge.server.layers import load_config
from mindie_knowledge.server.query import query


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--count", type=int, default=10000)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    entries, references = [], []
    started = time.perf_counter()
    for number in range(args.count):
        raw = (f"# Ascend graph observation {number}\n\n## Conditions\n"
               "Model: Gemma\nDevice: Ascend NPU\nMode: ACLGraph\n\n"
               "The Python torch operator reference names npu_gemma_rms_norm. "
               "Its C++ adapter names aclnnGemmaRmsNorm through dynamic dispatch. "
               "These are static source observations, not execution evidence.\n")
        alias = f"gemma-observation-{number:06d}"
        text, row = prepare_reference(f"cases/{number:06d}.md", raw, {"retrieval": {
            "source_sha256": normalized_sha256(raw), "aliases": [alias], "topics": ["ascend"]}})
        data = text.encode()
        import hashlib
        entries.append({"path": row["path"], "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "text": text})
        references.append(row)
    pack = root / "fixture.ovpack"
    index = make_pack(pack, entries)
    data = make_manifest(pack, entries, index)
    reference_path = root / "fixture.references.json"
    data["references"] = write_references(reference_path, source_git_sha=GIT_SHA, documents=references)
    manifest = validate_release_manifest(data, expected=ExpectedContract())
    state = DistributionState(root / "state")
    prepared = prepare_mount(state, manifest, pack, references_path=reference_path)
    pointer = {"schema": CURRENT_SCHEMA, "version_id": manifest.version_id, "source_git_sha": GIT_SHA,
               "root_uri": f"viking://resources/shared/{manifest.version_id}", "manifest_path": str(root / "release.json"), **prepared}
    atomic_write_json(root / "release.json", data)
    state.write_current(pointer)
    phases = {"prepare_seconds": round(time.perf_counter() - started, 3)}
    del entries, references, data, row, text, raw, index
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    observed = sum(1 for _ in prepared_shared_documents(root / "state", current=pointer))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    phases.update(stream_seconds=round(time.perf_counter() - started, 3), stream_peak_python_bytes=peak, streamed_documents=observed)
    bootstrap = root / "bootstrap"
    bootstrap.mkdir()
    config = load_config({"state_root": str(root / "state"), "backend": "memory", "layers": {
        "shared": str(bootstrap), "project": {"enabled": False}, "candidate": {"enabled": False}}}, env={})
    config.retrieval = MemoryBackend()
    started = time.perf_counter()
    catalog = refresh_catalog(config, extra_documents=prepared_shared_documents(root / "state", current=pointer))
    phases["catalog_seconds"] = round(time.perf_counter() - started, 3)
    assert catalog["status"] == "ready", catalog
    samples = []
    for position in [0, args.count // 2, args.count - 1] * 12:
        alias = f"gemma-observation-{position:06d}"
        started = time.perf_counter()
        result = query(config, text=f"topic:ascend {alias}", limit=3).to_dict()
        samples.append((time.perf_counter() - started) * 1000)
        assert result["results"] and result["results"][0]["uri"].endswith(f"/{position:06d}.md"), result
    ordered = sorted(samples[3:])
    report = {"count": args.count, "source_kind": "synthetic VA-domain fixture on actual disk",
              "native_embedding": False, "phases": phases, "catalog": catalog,
              "query_ms": {"median": round(statistics.median(ordered), 3), "p95": round(ordered[int(.95 * (len(ordered)-1))], 3)},
              "query_output_bytes": len(json.dumps(result, ensure_ascii=False).encode())}
    (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "catalog"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
