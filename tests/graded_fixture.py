"""Synthetic protocol inputs for condition semantics; not hardware evidence."""
from copy import deepcopy
import hashlib

from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.graded.distill import _sha

CONDITIONS = {"soc": ["Ascend910B"], "cann": ">=8.0,<9", "framework": ["torch_npu>=2.5"], "workload_scope": "decode"}
OBSERVED = {"soc": "Ascend910B", "cann": "8.1", "framework": {"torch_npu": "2.5.1"}, "workload_scope": "decode"}


def unit(ident="unit-fixture-probe", *, conditions=None, parents=(), body="Fixture rule: compare eager and graph capture.", **updates):
    metadata = dict(schema="mindie-graded-unit/1", id=ident, tier="L2", category="sop",
                    evidence_strength="single_observation", domain_path="serving/graph", inherits_from=list(parents),
                    title="Fixture graph capture probe", triggers={"error_signatures": [], "keywords": ["graph", "capture"]},
                    conditions=deepcopy(CONDITIONS if conditions is None else conditions),
                    provenance={"source_type": "session_distill", "source_locator": "fixture@abcdef1:probe.txt",
                                "source_digest": hashlib.sha256(body.encode()).hexdigest(), "evidence_refs": ["fixture-observation"], "supersedes": []})
    metadata.update(updates)
    return GradedUnit.from_parts(metadata, body, f"{metadata['domain_path']}/{ident}.md")


def request(*, state="verified_milestone", outcome="observed", repetitions=1):
    item = unit()
    inputs = [{"kind": "authorized_transcript", "outcome": outcome,
               "source_locator": item.metadata["provenance"]["source_locator"],
               "source_digest": item.metadata["provenance"]["source_digest"],
               "content": item.body.strip(), "evidence_refs": ["fixture-observation"], "repetitions": repetitions}]
    value = {"schema_version": "mindie-distill-request/1", "session_digest": "a" * 64,
             "task_state": state, "evidence_inputs": ["authorized_transcript"],
             "sanitized_inputs": inputs, "redaction_rules": [], "redaction_completed": True, "model_calls": 0}
    value["request_digest"] = _sha(value)
    return value


def decision(req, value=None, operation="create"):
    value = value or unit()
    m = value.metadata
    return {"schema_version": "mindie-distill-decision/1", "session_digest": req["session_digest"],
            "evidence_inputs": req["evidence_inputs"], "action": "propose_graded_units", "drop_reason": None,
            "units": [{"target_unit_id": value.id, "target_tier": m["tier"], "category": m["category"],
                       "evidence_strength": m["evidence_strength"], "domain_path": m["domain_path"], "operation": operation,
                       "frontmatter": deepcopy(m), "markdown_body": value.body}]}
