"""Business-semantics tests for DS-ENG06/C8 graded maintenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mindie_knowledge.graded.index import RoutingIndex, read_units
from mindie_knowledge.graded.maintenance import (
    apply_move_plan,
    build_routing_index,
    factor_proposals,
    lint_corpus,
    lint_main,
    mechanical_duplicates,
    semantic_merge_reviews,
    shard_plan,
)
from mindie_knowledge.graded.units import GradedUnit

RULE = "When the collective times out, drain the queue before retrying the operation.\n"


def _provenance(locator: str, digest: str = "a" * 64) -> dict:
    return {
        "source_type": "task",
        "source_locator": f"tasks/{locator}/index.md",
        "source_digest": digest,
        "evidence_refs": [f"tasks/{locator}/blocks/1.md"],
    }


def _unit(
    path: str,
    *,
    ident: str,
    body: str = RULE,
    conditions: dict | None = None,
    tier: str = "L2",
    category: str = "sop",
    evidence_strength: str = "single_observation",
    provenance: dict | None = None,
    triggers: dict | None = None,
    domain_path: str = "ascend/operations",
    inherits_from: list[str] | None = None,
) -> GradedUnit:
    metadata = {
        "schema": "mindie-graded-unit/1",
        "id": ident,
        "tier": tier,
        "category": category,
        "evidence_strength": evidence_strength,
        "title": ident.replace("-", " ").title(),
        "domain_path": domain_path,
        "inherits_from": list(inherits_from or []),
        "conditions": conditions or {"chip": ["ascend-910b"]},
        "triggers": triggers or {
            "error_signatures": ["HCCL_TIMEOUT"],
            "keywords": ["hccl", "timeout", "collective"],
        },
        "provenance": provenance or _provenance(ident),
    }
    return GradedUnit.from_parts(metadata, body, path)


def _write(root: Path, unit: GradedUnit) -> Path:
    target = root / unit.path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(unit.render(), encoding="utf-8", newline="\n")
    return target


def _make_corpus(root: Path, *units: GradedUnit) -> None:
    for unit in units:
        _write(root, unit)


def test_different_chip_lookalikes_are_never_mechanically_merged(tmp_path: Path) -> None:
    a = _unit(
        "ascend/910b/unit-timeout-910b.md",
        ident="unit-timeout-910b",
        conditions={"chip": ["ascend-910b"], "framework": ["torch==2.1.0"]},
        domain_path="ascend/910b",
    )
    b = _unit(
        "ascend/310p/unit-timeout-310p.md",
        ident="unit-timeout-310p",
        conditions={"chip": ["ascend-310p"], "framework": ["torch==2.1.0"]},
        domain_path="ascend/310p",
        provenance=_provenance("310p-task"),
    )
    _make_corpus(tmp_path, a, b)

    report = lint_corpus(tmp_path)

    assert report["valid"] is True
    assert report["mechanical_duplicates"] == []
    assert report["counts"]["mechanical_duplicate_groups"] == 0
    assert len(report["semantic_merge_reviews"]) == 1
    proposal = report["semantic_merge_reviews"][0]
    assert proposal["type"] == "propose_semantic_merge_review"
    assert proposal["unit_ids"] == ["unit-timeout-310p", "unit-timeout-910b"]
    assert proposal["automatic"] is False
    assert "chip" in proposal["condition_diff_keys"]
    assert "hccl timeout" in proposal["shared_triggers"]


def test_same_rule_with_different_provenance_is_retained_as_distinct_evidence(tmp_path: Path) -> None:
    a = _unit(
        "rules/unit-drain-a.md",
        ident="unit-drain-a",
        provenance=_provenance("task-a", "a" * 64),
    )
    b = _unit(
        "rules/unit-drain-b.md",
        ident="unit-drain-b",
        provenance=_provenance("task-b", "b" * 64),
    )
    _make_corpus(tmp_path, a, b)

    report = lint_corpus(tmp_path)

    assert report["valid"] is True
    assert len(report["mechanical_duplicates"]) == 1
    group = report["mechanical_duplicates"][0]
    assert group["disposition"] == "retain_distinct_evidence"
    assert group["evidence_preserved"] is True
    assert group["automatic"] is False
    assert set(group["unit_ids"]) == {"unit-drain-a", "unit-drain-b"}
    assert len(group["provenance_variants"]) == 2
    assert {variant["source_locator"] for variant in group["provenance_variants"]} == {
        "tasks/task-a/index.md",
        "tasks/task-b/index.md",
    }
    assert group["proposals"][0]["type"] == "propose_evidence_preserving_merge"


def test_missing_inheritance_reference_is_visible_and_blocks_index(tmp_path: Path) -> None:
    child = _unit(
        "rules/unit-child.md",
        ident="unit-child",
        inherits_from=["unit-missing"],
    )
    _make_corpus(tmp_path, child)

    report = lint_corpus(tmp_path)
    build = build_routing_index(tmp_path)

    assert report["valid"] is False
    assert report["errors"]
    assert any("missing inherited unit" in item["message"] for item in report["errors"])
    assert build["ok"] is False
    assert build["written"] is False
    assert "missing inherited unit" in build["errors"][0]["message"]
    assert not (tmp_path / "index" / "routing-manifest.json").exists()


def test_inheritance_cycle_is_visible_and_blocks_index(tmp_path: Path) -> None:
    a = _unit("rules/unit-a.md", ident="unit-a", inherits_from=["unit-b"])
    b = _unit("rules/unit-b.md", ident="unit-b", inherits_from=["unit-a"])
    _make_corpus(tmp_path, a, b)

    report = lint_corpus(tmp_path)
    build = build_routing_index(tmp_path)

    assert report["valid"] is False
    assert "inheritance cycle" in report["errors"][0]["message"]
    assert build["ok"] is False
    assert "inheritance cycle" in build["errors"][0]["message"]


def test_moving_a_file_retains_stable_id_content_and_references(tmp_path: Path) -> None:
    parent = _unit("rules/unit-parent.md", ident="unit-parent")
    child = _unit("rules/unit-child.md", ident="unit-child", inherits_from=["unit-parent"])
    _make_corpus(tmp_path, parent, child)
    assert build_routing_index(tmp_path)["ok"] is True

    before = {unit.id: unit for unit in read_units(tmp_path)}
    assert RoutingIndex(before.values()).closure["unit-child"] == ["unit-parent"]
    parent_sha = before["unit-parent"].content_sha256

    plan = {
        "schema": "mindie-shard-apply-plan/1",
        "operations": [{
            "action": "move",
            "id": "unit-parent",
            "from": "rules/unit-parent.md",
            "to": "rules/common/unit-parent.md",
            "content_sha256": parent_sha,
        }],
    }
    applied = apply_move_plan(tmp_path, plan)

    assert applied["applied"] is True
    assert applied["automatic_splits"] is False
    assert applied["cross_repo_writes"] is False
    assert applied["verified"] is True
    assert not (tmp_path / "rules" / "unit-parent.md").exists()
    assert (tmp_path / "rules" / "common" / "unit-parent.md").is_file()

    after = {unit.id: unit for unit in read_units(tmp_path)}
    index = RoutingIndex(after.values())
    assert after["unit-parent"].id == "unit-parent"
    assert after["unit-parent"].content_sha256 == parent_sha
    assert after["unit-parent"].path == "rules/common/unit-parent.md"
    assert after["unit-parent"].metadata["domain_path"] == "rules/common"
    assert after["unit-parent"].revision == before["unit-parent"].revision
    from mindie_knowledge.graded.contribution import parse_unit
    assert parse_unit(after["unit-parent"].render(), after["unit-parent"].path).id == "unit-parent"
    assert index.closure["unit-child"] == ["unit-parent"]

    rebuild = build_routing_index(tmp_path)
    manifest = json.loads((tmp_path / "index" / "routing-manifest.json").read_text(encoding="utf-8"))
    assert rebuild["ok"] is True
    assert manifest["id_path"]["unit-parent"] == "rules/common/unit-parent.md"
    assert manifest["closure"]["unit-child"] == ["unit-parent"]


def test_apply_plan_prevalidates_every_move_before_writing(tmp_path: Path) -> None:
    first = _unit("rules/unit-first.md", ident="unit-first")
    _make_corpus(tmp_path, first)
    plan = {
        "schema": "mindie-shard-apply-plan/1",
        "operations": [
            {"action": "move", "id": "unit-first", "from": "rules/unit-first.md", "to": "other/first.md"},
            {"action": "move", "id": "unit-gone", "from": "rules/unit-gone.md", "to": "other/gone.md"},
        ],
    }

    report = apply_move_plan(tmp_path, plan)

    assert report["applied"] is False
    assert report["partial"] is False
    assert report["errors"]
    assert (tmp_path / "rules" / "unit-first.md").is_file()
    assert not (tmp_path / "other" / "first.md").exists()


def test_failed_lint_leaves_previous_manifest_unchanged(tmp_path: Path) -> None:
    good = _unit("rules/unit-good.md", ident="unit-good")
    _make_corpus(tmp_path, good)
    first = build_routing_index(tmp_path)
    assert first["ok"] is True and first["written"] is True
    manifest_path = tmp_path / "index" / "routing-manifest.json"
    topology_path = tmp_path / "index" / "shard-topology.json"
    manifest_before = manifest_path.read_bytes()
    topology_before = topology_path.read_bytes()

    (tmp_path / "rules" / "unit-bad.md").write_text(
        "---\n"
        "schema: mindie-graded-unit/1\n"
        "id: unit-bad\n"
        "tier: L2\n"
        "category: sop\n"
        "title: Bad unit\n"
        "domain_path: ascend/operations\n"
        "---\n"
        "A malformed rule with no conditions or provenance.\n",
        encoding="utf-8",
        newline="\n",
    )

    assert lint_corpus(tmp_path)["valid"] is False
    failed = build_routing_index(tmp_path)

    assert failed["ok"] is False
    assert failed["written"] is False
    assert manifest_path.read_bytes() == manifest_before
    assert topology_path.read_bytes() == topology_before


def test_routing_index_writes_stable_mapping_and_check(tmp_path: Path) -> None:
    parent = _unit("rules/unit-parent.md", ident="unit-parent")
    child = _unit("rules/unit-child.md", ident="unit-child", inherits_from=["unit-parent"])
    _make_corpus(tmp_path, parent, child)

    build = build_routing_index(tmp_path)
    manifest_path = tmp_path / "index" / "routing-manifest.json"
    topology_path = tmp_path / "index" / "shard-topology.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    topology = json.loads(topology_path.read_text(encoding="utf-8"))

    assert build["ok"] is True and build["written"] is True
    assert manifest["schema_version"] == "mindie-routing-manifest/1"
    assert manifest["unit_count"] == 2
    assert manifest["id_path"] == {
        "unit-child": "rules/unit-child.md",
        "unit-parent": "rules/unit-parent.md",
    }
    assert manifest["closure"]["unit-child"] == ["unit-parent"]
    assert "digest" not in manifest
    assert manifest["build_elapsed_ms"] >= 0
    assert "index_digest" not in topology
    assert topology["closure"]["unit-child"] == ["unit-parent"]
    assert topology["units"]["unit-parent"]["path"] == "rules/unit-parent.md"
    assert topology["leaves"][0]["unit_ids"] == ["unit-child", "unit-parent"]
    assert list((tmp_path / "index").glob(".*.tmp")) == []

    checked = build_routing_index(tmp_path, check=True)
    assert checked["ok"] is True
    assert checked["written"] is False


def test_check_detects_a_tampered_manifest_without_rewriting_it(tmp_path: Path) -> None:
    _make_corpus(tmp_path, _unit("rules/unit-one.md", ident="unit-one"))
    assert build_routing_index(tmp_path)["ok"] is True
    manifest_path = tmp_path / "index" / "routing-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unit_count"] = 99
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tampered = manifest_path.read_bytes()

    checked = build_routing_index(tmp_path, check=True)

    assert checked["ok"] is False
    assert any("stale" in item["message"] for item in checked["errors"])
    assert manifest_path.read_bytes() == tampered


def test_empty_corpus_is_valid(tmp_path: Path) -> None:
    report = lint_corpus(tmp_path)

    assert report["valid"] is True
    assert report["errors"] == []
    assert report["counts"]["units"] == 0
    assert report["mechanical_duplicates"] == []
    assert report["semantic_merge_reviews"] == []
    assert report["factor_proposals"] == []


def test_tasks_feedback_git_and_private_history_are_skipped(tmp_path: Path) -> None:
    visible = _unit("rules/unit-visible.md", ident="unit-visible")
    _make_corpus(tmp_path, visible)
    for area in ("tasks", "feedback", ".git", ".private", "private-history"):
        hidden = _unit(f"{area}/unit-hidden-{area.replace('.', 'x')}.md",
                       ident=f"unit-hidden-{area.replace('.', 'x').replace('-', '-')}")
        _make_corpus(tmp_path, hidden)

    report = lint_corpus(tmp_path)

    assert report["valid"] is True
    assert report["counts"]["units"] == 1
    assert list(report["closure"]) == ["unit-visible"]


def test_invalid_graded_entry_fails_lint_visibly(tmp_path: Path, capsys) -> None:
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "unit-bad.md").write_text(
        "---\n"
        "schema: mindie-graded-unit/1\n"
        "id: unit-bad\n"
        "tier: L2\n"
        "category: sop\n"
        "title: Bad unit\n"
        "domain_path: ascend/operations\n"
        "---\n"
        "A rule without conditions, triggers or provenance.\n",
        encoding="utf-8",
        newline="\n",
    )

    report = lint_corpus(tmp_path)
    code = lint_main(["--root", str(tmp_path)])
    output = json.loads(capsys.readouterr().out)

    assert report["valid"] is False
    assert report["errors"]
    assert code == 1
    assert output["valid"] is False
    assert output["errors"]


def test_shard_plan_reports_threshold_candidates_without_applying_them() -> None:
    many_leaf_units = [
        _unit(f"domain/branch/leaf/unit-{index:02d}.md", ident=f"unit-{index:02d}",
              body=f"Rule number {index}.\n")
        for index in range(26)
    ]
    token_heavy = [
        _unit("domain/branch/heavy/unit-heavy-a.md", ident="unit-heavy-a", body="a" * 52000 + "\n"),
        _unit("domain/branch/heavy/unit-heavy-b.md", ident="unit-heavy-b", body="b" * 52000 + "\n"),
    ]
    many_subtree_units = [
        _unit(f"domain/branch/more/unit-{index:03d}.md", ident=f"unit-{index:03d}",
              body=f"Another rule number {index}.\n")
        for index in range(26, 125)
    ]

    plan = shard_plan(many_leaf_units + token_heavy + many_subtree_units)

    assert plan["dry_run"] is True
    assert plan["automatic_splits"] is False
    assert plan["cross_repo_writes"] is False
    leaves = {candidate["path"]: candidate for candidate in plan["candidates"] if candidate["kind"] == "leaf"}
    assert leaves["domain/branch/leaf"]["units"] == 26
    assert "units" in leaves["domain/branch/leaf"]["exceeds"]
    assert leaves["domain/branch/heavy"]["estimated_tokens"] > 25000
    assert "estimated_tokens" in leaves["domain/branch/heavy"]["exceeds"]
    subtrees = {
        candidate["path"]: candidate
        for candidate in plan["candidates"]
        if candidate["kind"] == "subtree"
    }
    assert subtrees["domain/branch"]["units"] == 127
    assert all(candidate["automatic"] is False for candidate in plan["candidates"])


def test_deep_hierarchical_subdivision_partitions_subtrees_cleanly() -> None:
    units = [
        _unit(f"operators/ascendc/optimizations/branch-{branch}/leaf-{leaf}/unit-{branch}-{leaf}-{idx}.md",
              ident=f"unit-{branch}-{leaf}-{idx}",
              body=f"Rule {branch} {leaf} {idx}.\n")
        for branch in range(3)
        for leaf in range(3)
        for idx in range(10)
    ]
    plan = shard_plan(units)
    assert plan["candidates"] == []


def test_federation_candidate_covers_the_corpus_boundary() -> None:
    units = [
        _unit(f"domain/leaf/unit-{index:04d}.md", ident=f"unit-{index:04d}",
              body=f"Federation rule {index}.\n")
        for index in range(5001)
    ]

    plan = shard_plan(units)
    federation = {candidate["path"]: candidate for candidate in plan["candidates"]
                  if candidate["kind"] == "federation"}

    assert federation["."]["units"] == 5001
    assert "units" in federation["."]["exceeds"]
    assert federation["domain"]["units"] == 5001
    assert federation["."]["automatic"] is False
    assert plan["automatic_splits"] is False


def test_factor_proposals_require_occurrence_floor_and_two_identical_rules() -> None:
    repeated = [
        _unit("rules/unit-repeat-a.md", ident="unit-repeat-a", body="Shared rule.\n"),
        _unit("rules/unit-repeat-b.md", ident="unit-repeat-b", body="Shared rule.\n",
              tier="L3", category="workflow", evidence_strength="hypothesis",
              provenance=_provenance("task-repeat-b")),
    ]
    unique = [
        _unit(f"rules/unit-unique-{index}.md", ident=f"unit-unique-{index}",
              body=f"Unique rule {index}.\n")
        for index in range(3)
    ]

    proposals = factor_proposals(repeated + unique)

    assert mechanical_duplicates(repeated) == []
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["occurrences"] == 2
    assert proposal["corpus_units"] == 5
    assert proposal["occurrence_ratio"] == 0.4
    assert proposal["preserves_conditions"] is True
    assert proposal["preserves_unit_id"] is True
    assert proposal["application"] == "explicit_reviewed_change"
    assert proposal["automatic"] is False

    below_floor = factor_proposals(repeated + unique + [
        _unit("rules/unit-extra-1.md", ident="unit-extra-1", body="Extra rule one.\n"),
        _unit("rules/unit-extra-2.md", ident="unit-extra-2", body="Extra rule two.\n"),
    ])
    assert below_floor == []


def test_mechanical_and_semantic_helpers_do_not_merge_incompatible_rules() -> None:
    same_rule = [
        _unit("rules/unit-same-a.md", ident="unit-same-a"),
        _unit("rules/unit-same-b.md", ident="unit-same-b",
              provenance=_provenance("same-b")),
    ]
    different_body = _unit("rules/unit-other.md", ident="unit-other",
                           body="A different rule with the same trigger.\n")

    assert len(mechanical_duplicates(same_rule)) == 1
    assert mechanical_duplicates([same_rule[0], different_body]) == []
    reviews = semantic_merge_reviews([same_rule[0], different_body])
    assert reviews == []


def test_apply_plan_rejects_factor_actions() -> None:
    report = apply_move_plan(Path("."), {
        "schema": "mindie-shard-apply-plan/1",
        "operations": [{"action": "factor", "id": "unit-a"}],
    })

    assert report["applied"] is False
    assert report["factor_application"] == "separate_reviewed_change"
    assert report["errors"]


def test_inheritance_difference_is_semantic_review_not_mechanical_dedup():
    first = _unit('ascend/a/unit-a.md', ident='unit-a', inherits_from=['unit-parent-a'])
    second = _unit('ascend/a/unit-b.md', ident='unit-b', inherits_from=['unit-parent-b'])
    assert mechanical_duplicates([first, second]) == []
    review = semantic_merge_reviews([first, second])
    assert len(review) == 1 and review[0]['inheritance_differs']


def test_move_rejects_unreadable_destination_before_mutating_source(tmp_path):
    value = _unit('ascend/operations/unit-move.md', ident='unit-move')
    source = _write(tmp_path, value)
    original = source.read_bytes()
    for destination in ('unit-move.md', 'ascend/.hidden/unit-move.md'):
        result = apply_move_plan(tmp_path, {'schema': 'mindie-shard-apply-plan/1', 'operations': [
            {'action': 'move', 'id': value.id, 'from': value.path, 'to': destination,
             'content_sha256': value.content_sha256}]})
        assert not result['applied'] and not result['partial'] and not result['moved']
        assert source.read_bytes() == original
        assert not (tmp_path / destination).exists()
