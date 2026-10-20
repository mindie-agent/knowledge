"""Real temp Git commits for the contract/2 graded publication validator."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from graded_fixture import unit
from mindie_knowledge.graded.index import RoutingIndex
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.publication_check import classify_changes, validate
from mindie_knowledge.publication_contract import (
    FORMATS, FORMATS_V2, GRADED_SCHEMA, SCHEMA, ContractMismatch, data_format,
    make_contract, parse_contract, render_contract,
)

DOMAIN = "vllm-ascend"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def publish(tmp_path, files=None, *, schema=GRADED_SCHEMA, domain=DOMAIN):
    files = dict(files or {})
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "check")
    git(repo, "config", "user.email", "check@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "core.eol", "lf")
    files["publication-contract.json"] = render_contract(make_contract(domain, "a" * 40, schema=schema))
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "publication")
    return repo, git(repo, "rev-parse", "HEAD")


def commit(repo, message="change"):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def revised(value, body):
    return GradedUnit.from_parts(
        {**value.metadata, "revision": value.revision + 1},
        body, f"{value.metadata['domain_path']}/{value.id}.md",
    )


def unit_files(*units):
    return {value.path: value.render() for value in units}


def manifest_for(units):
    index = RoutingIndex(units)
    manifest = index.manifest()
    manifest["id_path"] = {row["id"]: row["path"] for row in manifest["units"]}
    manifest["closure"] = {ident: list(parents) for ident, parents in index.closure.items()}
    return manifest


def test_contract_v2_selects_graded_formats_and_default_stays_v1():
    old = make_contract(DOMAIN, "a" * 40)
    assert old["schema"] == SCHEMA and old["formats"] == FORMATS
    assert data_format(old) == "material-package"
    graded = make_contract(DOMAIN, "a" * 40, schema=GRADED_SCHEMA)
    assert graded["schema"] == GRADED_SCHEMA and graded["formats"] == FORMATS_V2
    assert data_format(graded) == "graded"
    raw = render_contract(graded)
    assert parse_contract(raw, DOMAIN)["contract"] == graded
    crossed = dict(graded, formats=dict(FORMATS))
    with pytest.raises(ContractMismatch, match="formats"):
        parse_contract(json.dumps(crossed), DOMAIN)
    unknown = dict(graded, schema="mindie-publication-contract/3")
    with pytest.raises(ContractMismatch, match="schema"):
        parse_contract(json.dumps(unknown), DOMAIN)


def test_v2_units_and_inheritance_are_fully_validated(tmp_path):
    parent = unit("unit-parent")
    child = unit("unit-child", parents=["unit-parent"])
    repo, sha = publish(tmp_path, unit_files(parent, child))
    result = validate(repo, sha, DOMAIN)
    assert result["entries"] == 2
    assert result["data_format"] == "graded"
    assert result["feedback_files"] == 0


def test_v2_rejects_legacy_tasks_tree_instead_of_claiming_empty(tmp_path):
    repo, sha = publish(tmp_path, {"tasks/" + "a" * 64 + "/index.md": "old package\n"})
    with pytest.raises(ValueError, match="legacy tasks/"):
        validate(repo, sha, DOMAIN)


@pytest.mark.parametrize("damage", [
    "digest", "duplicate", "missing-parent", "path", "crlf", "revision", "executable",
    "nonunit-markdown", "private-path",
])
def test_v2_malformed_units_are_rejected(tmp_path, damage):
    parent = unit("unit-parent")
    files = unit_files(parent)
    if damage == "digest":
        path = parent.path
        files[path] = files[path].replace("content_digest: sha256:", "content_digest: sha256:" + "b" * 64 + " #")
    elif damage == "duplicate":
        files.update(unit_files(unit("unit-parent", domain_path="serving/other")))
    elif damage == "missing-parent":
        files.update(unit_files(unit("unit-orphan", parents=["unit-absent"])))
    elif damage == "path":
        files["elsewhere/unit-parent.md"] = files.pop(parent.path)
    elif damage == "crlf":
        files[parent.path] = files[parent.path].replace("\n", "\r\n")
    elif damage == "revision":
        files[parent.path] = files[parent.path].replace("revision: 1", "revision: zero")
    elif damage == "nonunit-markdown":
        files["notes/readme.md"] = "# ordinary documentation is not a unit\n"
    elif damage == "private-path":
        files.update(unit_files(unit("unit-parent", domain_path="private/hidden")))
    repo, sha = publish(tmp_path, files)
    if damage == "executable":
        git(repo, "add", "--chmod=+x", parent.path)
        git(repo, "commit", "-qm", "executable unit")
        sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        validate(repo, sha, DOMAIN)


def test_v2_derived_data_is_validated_but_units_remain_authoritative(tmp_path):
    value = unit("unit-parent")
    manifest = manifest_for([value])
    files = {**unit_files(value), "index/routing-manifest.json": json.dumps(manifest, sort_keys=True) + "\n"}
    repo, sha = publish(tmp_path, files)
    assert validate(repo, sha, DOMAIN)["entries"] == 1

    # A supported derived document cannot invent a unit or hide one.
    lying = dict(manifest, unit_count=2)
    (repo / "index/routing-manifest.json").write_text(json.dumps(lying, sort_keys=True) + "\n", encoding="utf-8")
    sha = commit(repo, "lying manifest")
    with pytest.raises(ValueError, match="unit_count|manifest"):
        validate(repo, sha, DOMAIN)

    unsupported = dict(manifest, schema_version="mindie-routing-manifest/9")
    (repo / "index/routing-manifest.json").write_text(json.dumps(unsupported, sort_keys=True) + "\n", encoding="utf-8")
    sha = commit(repo, "unsupported manifest")
    with pytest.raises(ValueError, match="unsupported derived schema"):
        validate(repo, sha, DOMAIN)

    (repo / "index/routing-manifest.json").unlink()
    (repo / "index/shard-topology.json").write_text(
        json.dumps({"schema": "mindie-shard-topology/9"}, sort_keys=True) + "\n", encoding="utf-8")
    sha = commit(repo, "unsupported topology")
    with pytest.raises(ValueError, match="unsupported derived schema"):
        validate(repo, sha, DOMAIN)


def test_v2_change_classes_cover_updates_moves_removals_and_contract(tmp_path):
    value = unit("unit-parent")
    repo, base = publish(tmp_path, unit_files(value))

    updated = revised(value, "Updated synthetic rule; no hardware claim.\n")
    path = repo / updated.path
    path.write_text(updated.render(), encoding="utf-8", newline="\n")
    head = commit(repo, "update")
    checked = validate(repo, head, DOMAIN, base_revision=base)
    assert checked["content_only"] is True and checked["review_mode"] == "content"

    moved = revised(
        GradedUnit.from_parts({**updated.metadata, "domain_path": "serving/other"},
                              updated.body, "serving/other/unit-parent.md"), updated.body)
    path.unlink()
    moved_path = repo / moved.path
    moved_path.parent.mkdir(parents=True, exist_ok=True)
    moved_path.write_text(moved.render(), encoding="utf-8", newline="\n")
    head = commit(repo, "move")
    checked = validate(repo, head, DOMAIN, base_revision=base)
    assert checked["entries"] == 1 and checked["content_only"] is True

    moved_path.unlink()
    head = commit(repo, "remove")
    checked = validate(repo, head, DOMAIN, base_revision=base)
    assert checked["entries"] == 0 and checked["content_only"] is True

    (repo / "AGENTS.md").write_text("Maintainer policy change.\n", encoding="utf-8")
    head = commit(repo, "policy")
    checked = validate(repo, head, DOMAIN, base_revision=base)
    assert checked["content_only"] is False
    assert checked["development_paths"] == ["AGENTS.md"]


def test_v2_contract_cannot_silently_select_the_v1_tree(tmp_path):
    value = unit("unit-parent")
    repo, sha = publish(tmp_path, unit_files(value), schema=SCHEMA)
    with pytest.raises(ValueError, match="unsupported content layout"):
        validate(repo, sha, DOMAIN)


def test_v2_checkpoint_reuses_unchanged_unit_but_revalidates_the_graph(tmp_path):
    parent = unit("unit-parent")
    child = unit("unit-child", parents=["unit-parent"])
    repo, first = publish(tmp_path, unit_files(parent, child))
    state = tmp_path / "validator-state.json"
    assert validate(repo, first, DOMAIN, state=state)["entries"] == 2

    (repo / parent.path).unlink()
    second = commit(repo, "delete parent only")
    with pytest.raises(ValueError, match="missing inherited unit"):
        validate(repo, second, DOMAIN, state=state)


def test_v2_feedback_uses_the_graded_schema_and_vote_fields(tmp_path):
    from mindie_knowledge.graded.feedback import SCHEMA_FEEDBACK
    value = unit("unit-parent")
    vote = {"vote_id": "vote-v2", "root_id": "root-v2", "unit_id": value.id,
            "revision": value.revision, "content_digest": value.metadata["content_digest"],
            "rating": "up", "reason": "Synthetic protocol observation."}
    files = {**unit_files(value),
             "feedback/votes.json": json.dumps({"schema": SCHEMA_FEEDBACK, "votes": [vote]}) + "\n"}
    repo, sha = publish(tmp_path, files)
    checked = validate(repo, sha, DOMAIN)
    assert checked["entries"] == 1 and checked["feedback_files"] == 1

    malformed = {**files, "feedback/votes.json": json.dumps(
        {"schema": SCHEMA_FEEDBACK, "votes": [dict(vote, revision=0)]}) + "\n"}
    repo2, sha2 = publish(tmp_path / "bad", malformed)
    with pytest.raises(ValueError, match="revision"):
        validate(repo2, sha2, DOMAIN)


def test_classify_changes_keeps_v1_immutable_block_rule(tmp_path):
    from test_publication_check import task_files
    repo, base = publish(tmp_path, task_files(), schema=SCHEMA)
    path = next(path for path in task_files() if "/blocks/" in path)
    file = repo / path
    changed = file.read_text().replace("Complete public evidence.", "Changed evidence.")
    file.write_text(changed)
    head = commit(repo, "rewrite block")
    with pytest.raises(ValueError, match="immutable block changed"):
        classify_changes(repo, base, head, schema=SCHEMA)


def test_v2_privacy_scan_sees_unit_metadata_and_body(tmp_path, monkeypatch):
    import re
    import mindie_knowledge.redact as redact_mod
    first = redact_mod.RULES[0]
    tightened = type(first)(id=first.id, description=first.description, hint=first.hint,
                            pattern=re.compile(r'policy-canary-\d+'))
    monkeypatch.setattr(redact_mod, 'RULES', (tightened,) + redact_mod.RULES[1:])
    value = unit('unit-parent', body='Body contains policy-canary-2; no hardware claim.\n')
    repo, sha = publish(tmp_path, unit_files(value))
    with pytest.raises(ValueError, match='privacy scan'):
        validate(repo, sha, DOMAIN)
