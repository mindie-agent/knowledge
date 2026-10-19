"""Authored factoring and withdrawal preserve claims across a real local tree."""
import pytest

from graded_fixture import unit
from mindie_knowledge.graded.index import RoutingIndex
from mindie_knowledge.graded.maintenance import build_routing_index
from mindie_knowledge.graded.reviewed_change import SCHEMA, apply_change, prepare_change
from mindie_knowledge.graded.units import GradedUnit


def write(root, *units):
    for value in units:
        path = root / value.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.render(), encoding="utf-8")


def change(old, new=None):
    value = dict(id=(old or new).id, expected_content_sha256=old.content_sha256 if old else None)
    return {**value, **(dict(path=new.path, content=new.render()) if new else dict(content=None))}


def plan(root, changes):
    return dict(schema=SCHEMA, review_reference="fixture:independent-editorial-review",
                base_generation=RoutingIndex.from_root(root).generation, changes=changes)


def test_reviewed_factor_updates_children_and_rejects_dangling_withdrawal(tmp_path):
    conditions = {"repository": "fixture/engine", "revision": "a" * 40}
    left = unit("unit-left", conditions=conditions, body="Shared premise. Inspect left queue.")
    right = unit("unit-right", conditions=conditions, body="Shared premise. Inspect right queue.")
    write(tmp_path, left, right)
    common = unit("unit-premise", conditions=conditions, domain_path="_common", body="Shared premise.")
    revised = [GradedUnit.from_parts({**old.metadata, "revision": 2, "inherits_from": [common.id]},
                                    "Inspect " + side + " queue.", old.path)
               for old, side in [(left, "left"), (right, "right")]]
    authored = plan(tmp_path, [change(None, common), change(left, revised[0]), change(right, revised[1])])
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.md")}
    prepared = prepare_change(tmp_path, authored)
    assert prepared["after"].closure[left.id] == [common.id]
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.md")} == before
    result = apply_change(tmp_path, authored)
    assert result["status"] == "applied" and result["publication"] == "not_requested"
    current = RoutingIndex.from_root(tmp_path)
    assert current.units[left.id].revision == 2
    assert current.units[left.id].content_sha256 != left.content_sha256
    assert current.units[common.id].metadata["conditions"] == conditions
    answer = current.query("left queue", conditions=conditions, limit=1)
    assert answer["results"][0]["inherited_rules"][0]["content"] == common.body
    assert current.query("left queue", conditions={**conditions, "revision": "b" * 40})["results"] == []
    with pytest.raises(ValueError, match="missing inherited unit"):
        prepare_change(tmp_path, plan(tmp_path, [change(common)]))
    withdrawn = apply_change(tmp_path, plan(tmp_path, [change(current.units[left.id]),
                                                    change(current.units[right.id]), change(common)]))
    assert withdrawn["status"] == "applied"
    assert RoutingIndex.from_root(tmp_path).units == {}
    assert build_routing_index(tmp_path, check=True)["ok"]


def test_stale_review_never_replaces_newer_edits_and_path_move_keeps_ref(tmp_path):
    original = unit()
    write(tmp_path, original)
    moved = GradedUnit.from_parts({**original.metadata, "domain_path": "serving/reviewed"},
                                 original.body, "serving/reviewed/" + original.id + ".md")
    authored = plan(tmp_path, [change(original, moved)])
    newer = GradedUnit.from_parts({**original.metadata, "revision": 2}, original.body + "New observation.", original.path)
    write(tmp_path, newer)
    with pytest.raises(ValueError, match="changed after review"):
        apply_change(tmp_path, authored)
    assert RoutingIndex.from_root(tmp_path).units[original.id].content_sha256 == newer.content_sha256
    moved_newer = GradedUnit.from_parts({**newer.metadata, "domain_path": "serving/reviewed"},
                                       newer.body, moved.path)
    result = apply_change(tmp_path, plan(tmp_path, [change(newer, moved_newer)]))
    assert result["status"] == "applied"
    after = RoutingIndex.from_root(tmp_path).units[original.id]
    assert after.content_sha256 == newer.content_sha256 and after.revision == newer.revision
    assert not (tmp_path / original.path).exists()
