"""Run the checked-in VA retrieval cases, including an enrichment ablation.

The authored questions and aliases are regression fixtures, not independently
labelled held-out production data. This runner uses the public query path with
vectors disabled so it does not attribute memory fixtures to native embeddings.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mindie_knowledge.catalog import refresh_catalog
from mindie_knowledge.evaluation import evaluate
from mindie_knowledge.markdown import meta_path, normalized_sha256
from mindie_knowledge.server.layers import load_config


def run():
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "retrieval-evaluation.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="vaws-reference-evaluation-") as temporary:
        root = Path(temporary)
        notes = root / "notes"
        notes.mkdir()
        for document in fixture["documents"]:
            (notes / document["path"]).write_text(document["raw"], encoding="utf-8")
        config = load_config({"backend": "unavailable", "state_root": str(root / "state"),
                              "layers": {"shared": {"enabled": False}, "candidate": {"enabled": False}, "project": str(notes)}}, env={})
        refresh_catalog(config)
        baseline = evaluate(config, fixture["cases"])
        for document in fixture["documents"]:
            metadata = {"retrieval": {"source_sha256": normalized_sha256(document["raw"]),
                                       "aliases": document.get("aliases", []), "topics": document.get("topics", [])}}
            meta_path(notes / document["path"]).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        refresh_catalog(config)
        enriched = evaluate(config, fixture["cases"])
        return {"scope": __doc__.strip(), "baseline_kind": "unenriched catalog ablation; not an old package version",
                "without_enrichment": baseline, "source_bound_enrichment": enriched}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run()
    print(json.dumps({key: value["summary"] for key, value in report.items() if isinstance(value, dict)}, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
