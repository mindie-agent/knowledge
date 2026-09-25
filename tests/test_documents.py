"""Canonical mindie-entry/2 document mechanics."""

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


def entry(**overrides):
    fields = dict(
        entry_id="b" * 64,
        domain="vllm-ascend",
        kind="experience",
        title="ACL graph investigation",
        summary="Compare eager and graph execution before investigating capture.",
        content="First attempt failed at capture; the eager run isolated the mode.",
        conditions={"torch_npu_version": "2.10.0.post2"},
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


def test_absent_conditions_roundtrip_and_omit_when_empty():
    doc = entry(conditions={})
    rendered = render_entry(doc)
    assert "conditions" not in rendered  # omitted, never a placeholder
    parsed = parse_entry(rendered)
    assert parsed == doc and parsed["conditions"] == {}
    assert parsed["revision"] == doc["revision"]  # normalized empty is stable


def test_rendered_data_carries_no_private_or_legacy_fields():
    rendered = render_entry(entry())
    for leaked in ("revision", "producers", "sources", "status",
                   "retirement_reason", "owner"):
        assert f"{leaked}:" not in rendered
    with pytest.raises(ValueError, match="unknown entry fields"):
        parse_entry(rendered.replace("kind:", "owner: " + "a" * 64 + "\nkind:", 1))
    legacy = rendered.replace("schema: mindie-entry/2", "schema: mindie-entry/1")
    with pytest.raises(ValueError, match="unsupported entry schema"):
        parse_entry(legacy)  # unknown schema fails loudly


def test_duplicate_yaml_keys_are_rejected():
    rendered = render_entry(entry())
    dup = rendered.replace("title:", "title: Other\ntitle:", 1)
    with pytest.raises(ValueError, match="duplicate YAML key"):
        parse_entry(dup)


@pytest.mark.parametrize("value", ["null", "[]", "not-a-map", "2"])
def test_invalid_optional_conditions_are_not_discarded(value):
    raw = render_entry(entry(conditions={}))
    with pytest.raises(ValueError, match="conditions"):
        parse_entry(raw.replace("kind:", f"conditions: {value}\nkind:", 1))


def test_required_fields_missing_fail_with_a_validation_error():
    raw = render_entry(entry()).replace("domain: vllm-ascend\n", "")
    with pytest.raises(ValueError, match="required"):
        parse_entry(raw)


def test_noncanonical_body_is_rejected_not_silently_rewritten():
    doc = entry()
    bad = dict(doc, content=doc["content"] + "\n")
    with pytest.raises(ValueError, match="canonical"):
        documents.validate(bad)
    bad["revision"] = revision_of(bad)
    with pytest.raises(ValueError, match="canonical"):
        documents.validate(bad)  # even a self-consistent digest cannot rescue it
    # make_entry normalizes at admission, so its output always round-trips.
    assert parse_entry(render_entry(doc)) == doc


def test_revision_is_deterministic_and_tracks_the_body():
    doc = entry()
    same = entry()
    assert same["revision"] == doc["revision"]  # deterministic on any host
    other = entry(summary="Different summary.")
    assert other["revision"] != doc["revision"]
    body_changed = entry(content="A corrected detailed finding.")
    assert body_changed["revision"] != doc["revision"]
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
    assert doc["content"] in updated["content"]


def test_cumulative_body_past_64kib_continues_to_the_file_envelope():
    # There is no 64 KiB business cap on accumulated experience; a long entry
    # keeps growing and is bounded only by the per-file publication envelope
    # (the rendered canonical file, header included).
    doc = entry()
    chunk = "x" * (32 * 1024)
    for index in range(3):
        doc, appended = append_observation(doc, chunk, marker=f"{index:064x}")
        assert appended
    assert len(doc["content"].encode()) > 64 * 1024
    parse_entry(render_entry(doc))  # the accumulated body stays canonical
    over = entry()
    with pytest.raises(DraftFull):
        append_observation(over, "y" * documents.MAX_FILE_BYTES, marker="e" * 32)


def test_malformed_files_fail_loudly():
    doc = entry()
    with pytest.raises(ValueError, match="UTF-8|frontmatter"):
        parse_entry(b"\xff\xfe" + render_entry(doc).encode()[:20])
    with pytest.raises(ValueError, match="LF"):
        parse_entry(render_entry(doc).replace("\n", "\r\n"))
    with pytest.raises(ValueError, match="frontmatter"):
        parse_entry("# no frontmatter\n")


def test_frontmatter_metadata_envelope_is_utf8_bytes_everywhere():
    # A header under 64 Ki CHARACTERS but over 64 KiB in UTF-8 (non-BMP) is
    # refused at parse, create AND render — one byte envelope, all surfaces.
    padded = "é" * (64 * 1024)  # 2 bytes per char: 128 KiB of bytes
    raw = (
        "---\nconditions: {}\ndomain: vllm-ascend\nentry_id: " + "b" * 64
        + "\nkind: experience\nsummary: s\ntitle: " + padded + "\n---\n\nbody\n"
    )
    with pytest.raises(ValueError, match="metadata byte limit"):
        parse_entry(raw)
    # Over the byte envelope even in pure ASCII: create/render refuse what
    # parse would refuse (the old 75 KB ASCII gap).
    big_conditions = {f"key_{i:04d}": "v" * 500 for i in range(140)}
    with pytest.raises(ValueError, match="metadata byte limit"):
        entry(conditions=big_conditions)
    # A normal header stays well within the envelope.
    assert parse_entry(render_entry(entry())) == entry()
