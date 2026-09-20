"""Skill candidate selection, package validation, plugin PR publication."""

import json

import pytest

from mindie_knowledge.community.common import CommunityError, Deadline
from mindie_knowledge.community.skill import (
    material_digest,
    select_candidates,
    scan_skill_candidates,
    validate_openai_yaml,
    validate_skill_markdown,
    validate_skill_package,
)

from .conftest import (
    PLUGIN_REPO,
    grok_script,
    grok_calls,
    make_entry,
    make_remote,
    vote,
)


def _votes(doc, *root_ids, rating="up"):
    return {(doc["entry_id"], doc["revision"]): [
        vote(f"v-{r}", doc["entry_id"], doc["revision"], rating=rating, root_id=r)
        for r in root_ids
    ]}


def test_candidate_requires_two_independent_non_producer_ups():
    doc = make_entry(producers=["root-producer"])
    entries = {doc["entry_id"]: doc}
    assert select_candidates(entries, _votes(doc, "r1")) == []
    # The producer's own vote never counts toward the threshold.
    assert select_candidates(entries, _votes(doc, "root-producer", "r1")) == []
    found = select_candidates(entries, _votes(doc, "r1", "r2"))
    assert len(found) == 1 and found[0]["supporters"] == ["r1", "r2"]
    # Down votes are not supporters.
    mixed = _votes(doc, "r1", "r2")
    mixed[(doc["entry_id"], doc["revision"])][1]["rating"] = "down"
    assert select_candidates(entries, mixed) == []


def test_candidate_skips_retired_and_stale_revision():
    retired = make_entry(status="retired", retirement_reason="obsolete")
    entries = {retired["entry_id"]: retired}
    assert select_candidates(entries, _votes(retired, "r1", "r2")) == []
    doc = make_entry()
    stale = _votes(doc, "r1", "r2")
    doc2 = make_entry(content="newer revision")
    assert select_candidates({doc["entry_id"]: doc2}, stale) == []


def test_material_digest_covers_source_feedback_target():
    doc = make_entry()
    a = material_digest(doc, doc["revision"], ["r1", "r2"], None)
    assert a == material_digest(doc, doc["revision"], ["r2", "r1"], None)
    assert a != material_digest(doc, doc["revision"], ["r1", "r2"], "skill-x")
    assert a != material_digest(doc, doc["revision"], ["r1", "r3"], None)


GOOD_SKILL_MD = """---
name: npu-device-numbering
description: Map physical NPU devices to container-logical numbering.
source_entries:
  - ENTRYID
---

# Method

Check the physical mapping first, then number logically from zero.
Reference entry ENTRYID for the full case history.
"""


def test_skill_markdown_validation():
    doc = make_entry()
    text = GOOD_SKILL_MD.replace("ENTRYID", doc["entry_id"])
    meta = validate_skill_markdown(text)
    assert meta["name"] == "npu-device-numbering"
    assert meta["source_entries"] == [doc["entry_id"]]
    with pytest.raises(ValueError, match="implicit"):
        validate_skill_markdown(text.replace("description:", "allow_implicit_invocation: true\ndescription:"))
    with pytest.raises(ValueError, match="absolute paths"):
        validate_skill_markdown(text + "\nSee /Users/alice/secrets for more.\n")
    with pytest.raises(ValueError):
        validate_openai_yaml("allow_implicit_invocation: true\n")
    validate_openai_yaml("allow_implicit_invocation: false\n")


def test_skill_package_validation_rejects_bad_refs():
    doc = make_entry()
    good = {"schema": "mindie-skill/1", "slug": "npu-device-numbering",
            "skill_md": GOOD_SKILL_MD.replace("ENTRYID", doc["entry_id"]),
            "openai_yaml": "allow_implicit_invocation: false\n",
            "references": {"case.md": "Detail."}}
    package = validate_skill_package(good, doc["entry_id"])
    assert package["slug"] == "npu-device-numbering"
    with pytest.raises(CommunityError, match="source entry"):
        validate_skill_package({**good, "skill_md": GOOD_SKILL_MD}, doc["entry_id"])
    with pytest.raises(CommunityError, match="reference"):
        validate_skill_package({**good, "references": {"../x.md": "no"}}, doc["entry_id"])


def test_skill_scan_pending_without_plugin_repo(settings, state_dir, transport, tmp_path, monkeypatch):
    doc = make_entry()
    monkeypatch.setattr(
        "mindie_knowledge.community.skill._load_repo_state",
        lambda repo, ref, t, d: ({doc["entry_id"]: doc}, _votes(doc, "r1", "r2")),
    )
    settings["bot"] = {"grok_argv": grok_script(tmp_path, {
        "schema": "mindie-skill/1", "slug": "npu-device-numbering",
        "skill_md": GOOD_SKILL_MD.replace("ENTRYID", doc["entry_id"]),
        "openai_yaml": "allow_implicit_invocation: false\n", "references": {}})}
    result = scan_skill_candidates(settings, state_dir, transport=transport)
    assert result["candidates"] == 1
    assert result["results"][0]["status"] == "pending"
    assert "plugin repository" in result["results"][0]["detail"]
    # Same material is never attempted twice, even though it stayed pending.
    again = scan_skill_candidates(settings, state_dir, transport=transport)
    assert again["results"][0]["status"] == "pending"
    assert "not repeating" in again["results"][0]["detail"]
    assert grok_calls(tmp_path) == 1


def test_skill_scan_publishes_plugin_pr(settings, state_dir, tmp_path, monkeypatch):
    plugin_url = make_remote(tmp_path, "plugin")
    settings["dev_remotes"][PLUGIN_REPO] = plugin_url
    from mindie_knowledge.community.transport import FileTransport

    transport = FileTransport(state_dir / "dev-github.json", settings["dev_remotes"])
    settings["bot"] = {
        "plugin_repository": PLUGIN_REPO,
        "grok_argv": grok_script(tmp_path, {
            "schema": "mindie-skill/1", "slug": "npu-device-numbering",
            "skill_md": GOOD_SKILL_MD.replace("ENTRYID", "PLACEHOLDER"),
            "openai_yaml": "allow_implicit_invocation: false\n",
            "references": {"case.md": "Full case."}}),
    }
    doc = make_entry()
    # The generated SKILL.md references the real entry id.
    import mindie_knowledge.community.skill as skill_mod

    real_generate = skill_mod._generate_skill

    def generate(argv, candidate, existing, bot):
        data = real_generate(argv, candidate, existing, bot)
        data["skill_md"] = data["skill_md"].replace("PLACEHOLDER", candidate["entry"]["entry_id"])
        return data

    monkeypatch.setattr(skill_mod, "_generate_skill", generate)
    monkeypatch.setattr(
        skill_mod, "_load_repo_state",
        lambda repo, ref, t, d: ({doc["entry_id"]: doc}, _votes(doc, "r1", "r2")),
    )
    result = scan_skill_candidates(settings, state_dir, transport=transport)
    published = result["results"][0]
    assert published["status"] == "submitted", published
    prs = transport.list_open_pull_requests(PLUGIN_REPO, deadline=Deadline(60, 30))
    assert len(prs) == 1
    files = transport.pull_request_files(PLUGIN_REPO, prs[0]["number"], Deadline(60, 30))
    paths = {f["filename"] for f in files}
    assert paths == {"skills/npu-device-numbering/SKILL.md",
                     "skills/npu-device-numbering/agents/openai.yaml",
                     "skills/npu-device-numbering/references/case.md"}
    # A repeat scan updates nothing new: same material digest is recorded.
    again = scan_skill_candidates(settings, state_dir, transport=transport)
    assert again["results"][0]["detail"].startswith("same material")
