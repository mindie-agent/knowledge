"""Canonical mindie-entry/1 document mechanics."""

import pytest

from mindie_knowledge.loop import documents
from mindie_knowledge.loop.documents import (
    DraftFull,
    append_observation,
    make_entry,
    parse_entry,
    render_entry,
    revision_of,
)

PRODUCER = "a" * 64


def entry(**overrides):
    fields = dict(
        entry_id="b" * 64,
        domain="vllm-ascend",
        kind="experience",
        title="ACL graph investigation",
        summary="Compare eager and graph execution before investigating capture.",
        content="First attempt failed at capture; the eager run isolated the mode.",
        conditions={"CANN": "9"},
        sources=["https://example.com/docs@abc123"],
        producers=[PRODUCER],
    )
    fields.update(overrides)
    return make_entry(**fields)


def test_render_parse_roundtrip_is_exact():
    doc = entry()
    rendered = render_entry(doc)
    assert "\\r" not in rendered and "\r" not in rendered
    parsed = parse_entry(rendered)
    assert parsed == doc
    assert render_entry(parsed) == rendered
    assert parsed["revision"] == revision_of(parsed)


def test_noncanonical_body_is_rejected_not_silently_rewritten():
    doc = entry()
    bad = dict(doc, content=doc["content"] + "\n")
    with pytest.raises(ValueError, match="canonical"):
        documents.validate(bad)
    bad["revision"] = revision_of({k: v for k, v in bad.items() if k != "revision"})
    with pytest.raises(ValueError, match="canonical"):
        documents.validate(bad)  # even a self-consistent digest cannot rescue it
    # make_entry normalizes at admission, so its output always round-trips.
    assert parse_entry(render_entry(doc)) == doc


def test_revision_tracks_fields_and_excludes_only_revision():
    doc = entry()
    same = entry()
    assert same["revision"] == doc["revision"]
    other = entry(summary="Different summary.")
    assert other["revision"] != doc["revision"]
    tampered = parse_entry(render_entry(doc))
    tampered["title"] = "Renamed"
    with pytest.raises(ValueError, match="revision"):
        parse_entry(render_entry(dict(tampered, revision=doc["revision"])))


def test_append_observation_is_idempotent_and_bounded():
    doc = entry()
    updated, appended = append_observation(doc, "Later observation Y changed Z.", marker="f" * 32)
    assert appended and updated["revision"] != doc["revision"]
    again, appended = append_observation(updated, "Later observation Y changed Z.", marker="f" * 32)
    assert not appended and again == updated
    assert "Earlier" not in updated["content"] or doc["content"] in updated["content"]
    big = "x" * (64 * 1024)
    with pytest.raises(DraftFull):
        append_observation(updated, big, marker="e" * 32)


def test_malformed_files_fail_loudly():
    doc = entry()
    with pytest.raises(ValueError, match="UTF-8|frontmatter"):
        parse_entry(b"\xff\xfe" + render_entry(doc).encode()[:20])
    with pytest.raises(ValueError, match="LF"):
        parse_entry(render_entry(doc).replace("\n", "\r\n"))
    with pytest.raises(ValueError, match="frontmatter"):
        parse_entry("# no frontmatter\n")
