"""Existing candidate state must not bypass bounded reads or be overwritten by collisions."""
import json
from unittest.mock import patch

import pytest

from mindie_knowledge import markdown
from mindie_knowledge.server.capture import capture
from mindie_knowledge.server.layers import load_config


def test_capture_rejects_oversize_orphan_metadata_before_any_write(tmp_path):
    title = "Fresh observation"
    target = tmp_path / f"{markdown.document_slug(title)}.md"
    sidecar = markdown.meta_path(target)
    raw = json.dumps({"retained_human_field": "x" * markdown.MAX_METADATA_BYTES}).encode()
    sidecar.write_bytes(raw)
    config = load_config({"backend": "memory", "state_root": str(tmp_path / "state"),
                          "layers": {"candidate": str(tmp_path), "shared": {"enabled": False},
                                     "project": {"enabled": False}}, "publishing": {"enabled": False}}, env={})
    with patch.object(markdown, "_atomic_write_text", wraps=markdown._atomic_write_text) as writes:
        with pytest.raises(ValueError, match="read budget"):
            capture(title=title, content="New bounded evidence.", config=config, index=False)
    writes.assert_not_called()
    assert sidecar.read_bytes() == raw and not target.exists()


def test_save_rejects_oversize_existing_target_before_any_write(tmp_path):
    title = "Original observation"
    target = tmp_path / f"{markdown.document_slug(title)}.md"
    raw = b"# Retained human title\n\n" + b"x" * markdown.MAX_REFERENCE_BYTES
    target.write_bytes(raw)
    with patch.object(markdown, "_atomic_write_text", wraps=markdown._atomic_write_text) as writes:
        with pytest.raises(ValueError, match="read budget"):
            markdown.save_document(tmp_path, layer="candidate", title=title, content="New evidence.")
    writes.assert_not_called()
    assert target.read_bytes() == raw and len(list(tmp_path.glob("*.md"))) == 1


def test_capture_second_collision_preserves_both_existing_notes(tmp_path):
    title = "Original observation"
    ident = markdown.document_slug(title)
    first = tmp_path / f"{ident}.md"
    second = tmp_path / f"{ident}-{markdown.title_digest(title + ident)}.md"
    first.write_text("# Retained first human title\n\nFirst evidence.\n", encoding="utf-8")
    second.write_text("# Retained second human title\n\nSecond evidence.\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (first, second)}
    config = load_config({"backend": "memory", "state_root": str(tmp_path / "state"),
                          "layers": {"candidate": str(tmp_path), "shared": {"enabled": False},
                                     "project": {"enabled": False}}, "publishing": {"enabled": False}}, env={})
    with patch.object(markdown, "_atomic_write_text", wraps=markdown._atomic_write_text) as writes:
        with pytest.raises(ValueError, match="occupied"):
            capture(title=title, content="New evidence.", config=config, index=False)
    writes.assert_not_called()
    assert {path: path.read_bytes() for path in (first, second)} == before
    assert len(list(tmp_path.glob("*.md"))) == 2


def test_matching_secondary_identity_can_still_be_updated(tmp_path):
    title = "Original observation"
    ident = markdown.document_slug(title)
    first = tmp_path / f"{ident}.md"
    first.write_text("# Retained human title\n\nEarlier evidence.\n", encoding="utf-8")
    original = first.read_bytes()
    created = markdown.save_document(tmp_path, layer="candidate", title=title, content="First captured evidence.")
    updated = markdown.save_document(tmp_path, layer="candidate", title=title, content="Updated evidence.\r\nStill uncertain.")
    assert updated.path == created.path and updated.path != first
    assert updated.content == "Updated evidence.\nStill uncertain."
    assert first.read_bytes() == original and len(list(tmp_path.glob("*.md"))) == 2
