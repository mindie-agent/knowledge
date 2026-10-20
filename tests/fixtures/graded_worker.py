"""Native-process protocol fixture. No model call or hardware evidence."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mindie_knowledge.materials.summarizer import canonical, digest, outcome, policy_identity
from mindie_knowledge.graded.distill import compact_unit_body, validate_request

identity = policy_identity(model="fixture-no-model", effort="none", implementation={"fixture": "graded-worker-v1"})
identity["capabilities"] = ["mindie-distill-request/1"]
identity["fingerprint"] = digest({key: value for key, value in identity.items() if key != "fingerprint"})
if "--identity" in sys.argv:
    print(canonical(identity))
    raise SystemExit(0)

request = validate_request(json.load(sys.stdin))
marker = Path(os.environ["MINDIE_GRADED_FIXTURE_CALLS"])
marker.write_text(str(int(marker.read_text()) + 1) if marker.exists() else "1")
if failed_status := os.environ.get("MINDIE_GRADED_FIXTURE_STATUS"):
    print(canonical(outcome(request, status=failed_status, model_calls=1,
                            error="unknown" if failed_status == "outcome_unknown" else "native")))
    raise SystemExit(0)
source = request["sanitized_inputs"][0]
related = request.get("related_units") or []
revisable = next(
    (
        item
        for item in related
        if item.get("body_complete") is True and item.get("id") != "unit-related-existing"
    ),
    None,
)
if revisable is not None:
    domain_path = os.environ.get("MINDIE_GRADED_FIXTURE_DOMAIN_PATH", "serving/graph")
    strength = os.environ.get("MINDIE_GRADED_FIXTURE_STRENGTH", "single_observation")
    merged_raw = revisable["body_excerpt"].rstrip() + "\n\n" + "\n\n".join(
        item["content"].strip() for item in request["sanitized_inputs"] if item["content"].strip()
    )
    compacted_body = compact_unit_body(merged_raw)
    refs = [ref for item in request["sanitized_inputs"] for ref in item["evidence_refs"]]
    frontmatter = dict(
        schema="mindie-graded-unit/1",
        id=revisable["id"],
        tier=revisable["tier"],
        category=revisable["category"],
        evidence_strength=strength,
        title=revisable["title"],
        domain_path=domain_path,
        inherits_from=[],
        triggers=revisable.get("triggers") or {"error_signatures": [], "keywords": ["graph"]},
        conditions=revisable.get("conditions") or {"soc": ["Ascend910B"]},
        provenance=dict(
            source_type=os.environ.get("MINDIE_GRADED_FIXTURE_SOURCE_TYPE", "authorized_transcript"),
            source_locator=source["source_locator"],
            source_digest=source["source_digest"],
            evidence_refs=refs,
            supersedes=[],
        ),
    )
    decision = dict(
        schema_version="mindie-distill-decision/1",
        session_digest=request["session_digest"],
        evidence_inputs=request["evidence_inputs"],
        action="propose_graded_units",
        drop_reason=None,
        units=[
            dict(
                target_unit_id=frontmatter["id"],
                target_tier=revisable["tier"],
                category=revisable["category"],
                evidence_strength=strength,
                domain_path=domain_path,
                operation="merge_into_existing",
                frontmatter=frontmatter,
                markdown_body=compacted_body,
            )
        ],
    )
else:
    frontmatter = dict(schema="mindie-graded-unit/1", id="unit-fixture-native", tier="L2", category="sop",
                       evidence_strength="single_observation", title="Fixture graph probe", domain_path="serving/graph",
                       inherits_from=[], triggers={"error_signatures": [], "keywords": ["graph"]},
                       conditions={"soc": ["Ascend910B"]}, provenance=dict(source_type="session_distill",
                       source_locator=source["source_locator"], source_digest=source["source_digest"],
                       evidence_refs=source["evidence_refs"], supersedes=[]))
    decision = dict(schema_version="mindie-distill-decision/1", session_digest=request["session_digest"],
                    evidence_inputs=request["evidence_inputs"], action="propose_graded_units", drop_reason=None,
                    units=[dict(target_unit_id=frontmatter["id"], target_tier="L2", category="sop",
                                evidence_strength="single_observation", domain_path=frontmatter["domain_path"],
                                operation="create", frontmatter=frontmatter, markdown_body="Fixture graph probe; no hardware claim.")])
print(canonical(outcome(request, status="returned", result=decision, model_calls=1,
                        usage_known=False, elapsed_ms=1)))
