"""Contract tests for deterministic C6 graded publication preparation."""
from __future__ import annotations

import builtins
from copy import deepcopy
import json
import os
from pathlib import Path
import socket

import pytest

from mindie_knowledge.graded.index import RoutingIndex
from mindie_knowledge.graded.publication import (
    PrivacyRejected,
    PublicationError,
    prepare_decision,
)
from mindie_knowledge.graded.units import GradedUnit

SESSION = "f" * 64
DIGEST = "a" * 64


def frontmatter(unit_id="unit-ascend-oom-retry", **changes):
    value = {
        "schema": "mindie-graded-unit/1",
        "id": unit_id,
        "tier": "L2",
        "category": "sop",
        "evidence_strength": "single_observation",
        "domain_path": "vllm-ascend/troubleshooting",
        "title": "Retry after NPU out of memory",
        "inherits_from": [],
        "conditions": {"framework": ["vllm==0.9.0"], "device": "Ascend910B"},
        "triggers": {"error_signatures": ["NPU out of memory"], "keywords": ["OOM"]},
        "provenance": {
            "source_type": "session",
            "source_locator": "session:public-observation",
            "source_digest": DIGEST,
            "evidence_refs": ["evidence-001"],
        },
    }
    value.update(changes)
    return value


BODY = "Retry the request after the worker releases the failed device.\n"


def entry(fm, body=BODY, operation="create", target=None, **mirrors):
    value = {
        "target_tier": fm["tier"],
        "category": fm["category"],
        "evidence_strength": fm["evidence_strength"],
        "domain_path": fm["domain_path"],
        "operation": operation,
        "target_unit_id": target,
        "frontmatter": fm,
        "markdown_body": body,
    }
    value.update(mirrors)
    return value


def decision(units, *, action="propose_graded_units", evidence_inputs=None, drop_reason=""):
    return {
        "schema": "mindie-distill-decision/1",
        "action": action,
        "session_digest": SESSION,
        "evidence_inputs": ["evidence-001"] if evidence_inputs is None else evidence_inputs,
        "drop_reason": drop_reason,
        "units": units,
    }


def existing_unit(fm=None, body=BODY, path=None):
    fm = fm or frontmatter()
    path = path or f"{fm['domain_path']}/{fm['id']}.md"
    return GradedUnit.from_parts(fm, body, path)


def test_duplicate_create_retry_is_unchanged_and_deterministic():
    current = existing_unit()
    index = RoutingIndex([current])
    request = decision([entry(frontmatter())])

    first = prepare_decision(request, index)
    second = prepare_decision(request, index)

    assert first.status == "unchanged"
    assert first.writes == []
    assert first.reviews == []
    assert len(first.unchanged) == 1
    assert first.unchanged[0]["unit_id"] == current.id
    assert first.unchanged[0]["reason"] == "identical_current_bytes"
    assert first.ready_for_publication is False
    assert first.gitleaks_review_required is True
    assert first.as_json() == second.as_json()


def test_create_conflicting_existing_id_is_not_silently_overwritten():
    current = existing_unit()
    index = RoutingIndex([current])
    changed_body = BODY + "This is a different claim for the same stable ID.\n"

    with pytest.raises(PublicationError, match="create conflicts"):
        prepare_decision(decision([entry(frontmatter(), body=changed_body)]), index)


def test_changed_conditions_becomes_merge_review_only():
    current = existing_unit()
    index = RoutingIndex([current])
    changed = frontmatter()
    changed["conditions"] = {
        "framework": ["vllm==0.9.0"],
        "device": "Ascend910B",
        "topology": "multi-node",
    }
    changed["triggers"] = {"error_signatures": ["collective timeout"], "keywords": ["timeout"]}

    proposal = prepare_decision(
        decision([entry(changed, operation="merge_into_existing", target=current.id)]),
        index,
    )

    assert proposal.status == "needs_review"
    assert proposal.writes == []
    assert len(proposal.reviews) == 1
    review = proposal.reviews[0]
    assert review["operation"] == "propose_semantic_merge_review"
    assert review["target_unit_id"] == current.id
    assert "conditions differ from target" in review["reasons"]
    assert review["expected_content_sha256"] == current.content_sha256
    assert proposal.ready_for_publication is False


def test_overlapping_error_fingerprints_becomes_merge_review_only():
    current = existing_unit()
    index = RoutingIndex([current])
    changed = frontmatter(title="Second observation of the same failure")

    proposal = prepare_decision(
        decision([entry(changed, operation="merge_into_existing", target=current.id)]),
        index,
    )

    assert proposal.writes == []
    assert len(proposal.reviews) == 1
    assert "error fingerprints overlap target" in proposal.reviews[0]["reasons"]


def test_merge_preserves_provenance_and_binds_compare_and_swap():
    current = existing_unit()
    index = RoutingIndex([current])
    proposed = frontmatter(title="Retry after NPU out of memory, with a bounded backoff")
    proposed["triggers"] = {
        "error_signatures": ["device allocation timeout"],
        "keywords": ["OOM", "backoff"],
    }
    proposed["provenance"]["evidence_refs"] = ["evidence-001", "evidence-002"]
    proposed_body = BODY + "Wait for the release event before the retry.\n"

    proposal = prepare_decision(
        decision(
            [entry(proposed, body=proposed_body, operation="merge_into_existing", target=current.id)],
            evidence_inputs=["evidence-001", "evidence-002"],
        ),
        index,
        authorized_publication=True,
    )

    assert proposal.status == "proposed"
    assert len(proposal.writes) == 1
    write = proposal.writes[0]
    assert write["operation"] == "merge_into_existing"
    assert write["path"] == current.path
    assert write["expected_content_sha256"] == current.content_sha256
    assert write["content_sha256"] != current.content_sha256
    assert write["frontmatter"]["provenance"]["source_locator"] == current.metadata["provenance"]["source_locator"]
    assert write["frontmatter"]["provenance"]["source_digest"] == current.metadata["provenance"]["source_digest"]
    assert write["frontmatter"]["provenance"]["evidence_refs"] == ["evidence-001", "evidence-002"]
    assert write["frontmatter"]["inherits_from"] == current.metadata["inherits_from"]
    assert proposal.authorized_publication is True
    assert proposal.local_authorization == "authorized"
    assert proposal.remote_consent_granted is False
    assert proposal.ready_for_publication is False
    assert proposal.gitleaks_review_required is True
    assert proposal.privacy["gitleaks_review_required"] is True


def test_merge_duplicate_retry_is_unchanged():
    current = existing_unit()
    index = RoutingIndex([current])
    proposed = frontmatter()
    proposed["triggers"] = {"error_signatures": ["device allocation timeout"], "keywords": ["OOM"]}
    proposed_body = BODY + "Retry once after the device is released.\n"
    request = decision(
        [entry(proposed, body=proposed_body, operation="merge_into_existing", target=current.id)]
    )

    first = prepare_decision(request, index)
    assert len(first.writes) == 1
    applied = GradedUnit.from_parts(
        first.writes[0]["frontmatter"], first.writes[0]["markdown_body"], first.writes[0]["path"],
    )
    second = prepare_decision(request, RoutingIndex([applied]))

    assert second.writes == []
    assert second.status == "unchanged"
    assert len(second.unchanged) == 1


@pytest.mark.parametrize(
    "case",
    [
        "bad-unit-id",
        "path-traversal",
        "target-mismatch",
        "mirror-tier",
        "create-names-target",
        "missing-target",
    ],
)
def test_id_path_and_target_tampering_is_rejected(case):
    current = existing_unit()
    index = RoutingIndex([current])

    if case == "bad-unit-id":
        bad = frontmatter(unit_id="../unit-ascend-oom-retry")
        request = decision([entry(bad)])
    elif case == "path-traversal":
        bad = frontmatter(domain_path="../outside")
        request = decision([entry(bad)])
    elif case == "target-mismatch":
        other = frontmatter(unit_id="unit-other-stable-id")
        request = decision([entry(other, operation="merge_into_existing", target=current.id)])
    elif case == "mirror-tier":
        request = decision([entry(frontmatter(), target_tier="L3")])
    elif case == "create-names-target":
        other = frontmatter(unit_id="unit-create-other")
        request = decision([entry(other, target=current.id)])
    else:
        request = decision([
            entry(frontmatter(), operation="merge_into_existing", target="unit-absent-from-index"),
        ])

    with pytest.raises(PublicationError):
        prepare_decision(request, index)


@pytest.mark.parametrize("surface", ["title", "body", "provenance"])
def test_privacy_rejection_reports_rules_not_matched_values(surface):
    canary = "10.23.45.67"
    proposed = frontmatter()
    proposed_body = BODY
    if surface == "title":
        proposed["title"] = f"Failure observed from {canary}"
    elif surface == "body":
        proposed_body = BODY + f"Observed on {canary}.\n"
    else:
        proposed["provenance"]["source_locator"] = f"session:{canary}"

    with pytest.raises(PrivacyRejected) as caught:
        prepare_decision(decision([entry(proposed, body=proposed_body)]), RoutingIndex([]))

    message = str(caught.value)
    assert "ipv4-address" in message
    assert canary not in message
    assert canary not in repr(caught.value)
    assert "ipv4-address" in caught.value.rule_ids


def test_multi_unit_cyclic_proposal_is_rejected_without_any_write():
    first = frontmatter(unit_id="unit-cycle-first", inherits_from=["unit-cycle-second"])
    second = frontmatter(unit_id="unit-cycle-second", inherits_from=["unit-cycle-first"])
    request = decision([entry(first), entry(second)])

    with pytest.raises(PublicationError) as caught:
        prepare_decision(request, RoutingIndex([]))

    assert "cycle" in str(caught.value)
    assert caught.value.code == "graded_publication_rejected"


def test_noop_carries_no_writes():
    proposal = prepare_decision(
        decision([], action="noop", evidence_inputs=[], drop_reason="No durable rule was observed."),
        RoutingIndex([]),
    )

    assert proposal.status == "noop"
    assert proposal.writes == []
    assert proposal.reviews == []
    assert proposal.unchanged == []
    assert proposal.drop_reason == "No durable rule was observed."
    assert proposal.ready_for_publication is False
    assert proposal.remote_consent_granted is False


def test_prepare_decision_has_no_filesystem_or_network_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("prepare_decision performed external I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    proposal = prepare_decision(decision([entry(frontmatter())]), RoutingIndex([]))

    assert len(proposal.writes) == 1
    assert proposal.writes[0]["path"] == "vllm-ascend/troubleshooting/unit-ascend-oom-retry.md"
    assert json.loads(proposal.as_json())["ready_for_publication"] is False


def test_schema_version_and_mirrored_create_target_are_accepted():
    proposed = frontmatter()
    proposed["provenance"]["evidence_refs"] = ["tasks/public-observation/blocks/1.md"]
    request = decision([entry(proposed, target=proposed["id"])])
    request["schema_version"] = request.pop("schema")

    proposal = prepare_decision(request, RoutingIndex([]))

    assert proposal.decision_schema == "mindie-distill-decision/1"
    assert len(proposal.writes) == 1
    assert proposal.writes[0]["frontmatter"]["provenance"]["evidence_refs"] == [
        "tasks/public-observation/blocks/1.md",
    ]


def test_explicit_semantic_merge_review_never_writes():
    current = existing_unit()
    proposed = frontmatter(title="Alternative framing for human review")
    proposed["conditions"] = {"device": "Ascend910B"}

    proposal = prepare_decision(
        decision([
            entry(proposed, operation="propose_semantic_merge_review", target=current.id),
        ]),
        RoutingIndex([current]),
    )

    assert proposal.status == "needs_review"
    assert proposal.writes == []
    assert len(proposal.reviews) == 1
    assert proposal.reviews[0]["operation"] == "propose_semantic_merge_review"
    assert proposal.reviews[0]["reasons"] == ["decision requested semantic merge review"]
    assert proposal.reviews[0]["expected_content_sha256"] == current.content_sha256
