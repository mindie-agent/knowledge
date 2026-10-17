"""Publication while a product read handle is open.

``FILE_SHARE_DELETE`` on the reader is required and not sufficient. CPython
documents that ``os.replace`` / ``MoveFileExW`` still rejects an open
destination (https://github.com/python/cpython/issues/90161). These tests
call the product writers only. They do not assert that ``os.replace``
succeeds against an open Windows file.
"""

import json

import pytest

from mindie_knowledge import consent_store
from mindie_knowledge.loop import settings as settings_mod

from test_consent_store_contract import _product_read_stream

REPOSITORY = "mindie-agent/knowledge-vllm-ascend"


def test_held_product_read_keeps_old_community_json(tmp_path):
    """Community settings use their own writer, not ``consent_store``.

    ``settings.write`` then ``update_extensions`` must land the new document.
    The handle opened by the product read must still contain the complete
    pre-write JSON.
    """
    path = tmp_path / "community.json"
    root = tmp_path / "proj"
    root.mkdir()
    settings_mod.write(
        path, enabled=True, repository=REPOSITORY, project_roots=[root], fork="lane/fork",
    )
    old = path.read_bytes()
    stamp = str((tmp_path / "authority.json").resolve())
    held = _product_read_stream(consent_store, path)
    try:
        settings_mod.write(
            path, enabled=False, repository=REPOSITORY, project_roots=[root],
        )
        settings_mod.update_extensions(path, consent_config=stamp)
        held.seek(0)
        previous = held.read()
    finally:
        held.close()
    new = json.loads(path.read_bytes())
    old_doc = json.loads(old)
    assert previous == old
    assert old_doc["enabled"] is True
    assert "consent_config" not in old_doc
    assert new["enabled"] is False
    assert new["generation"] != old_doc["generation"]
    assert new["consent_config"] == stamp
    assert new["fork"] == "lane/fork"


def _winerror(code, path):
    exc = PermissionError(13, "The process cannot access the file because it "
                          "is being used by another process", str(path))
    exc.winerror = code
    return exc


def test_replace_retries_a_transient_sharing_holder(tmp_path, monkeypatch):
    """A plain-open reader (no FILE_SHARE_DELETE) fails WinError 32 briefly."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"new document")
    target.write_bytes(b"old document")
    calls = []

    def held_briefly(src, dst):
        calls.append(src)
        if len(calls) < 3:
            raise _winerror(32, dst)
        import os
        os.replace(src, dst)

    monkeypatch.setattr(consent_store, "_rename_windows_once", held_briefly)
    consent_store._replace_windows(source, target)
    assert len(calls) == 3
    assert target.read_bytes() == b"new document"
    assert not source.exists()


def test_replace_permanent_holder_reraises_the_original_sharing_error(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"new document")
    target.write_bytes(b"old document")
    calls = []

    def held_forever(src, dst):
        calls.append(src)
        raise _winerror(32, dst)

    clock = [100.0]
    monkeypatch.setattr(consent_store, "_rename_windows_once", held_forever)
    monkeypatch.setattr(consent_store.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(consent_store.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(PermissionError) as caught:
        consent_store._replace_windows(source, target)
    assert caught.value.winerror == 32
    assert 1 < len(calls) <= 2 + consent_store.RENAME_RETRY_SECONDS / consent_store.LOCK_POLL_SECONDS
    # Neither side is torn: the old document and the staged replacement survive.
    assert target.read_bytes() == b"old document"
    assert source.read_bytes() == b"new document"


def test_replace_non_sharing_error_is_never_retried(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"new document")
    target.write_bytes(b"old document")
    calls = []

    def denied(src, dst):
        calls.append(src)
        raise _winerror(5, dst)

    monkeypatch.setattr(consent_store, "_rename_windows_once", denied)
    with pytest.raises(PermissionError) as caught:
        consent_store._replace_windows(source, target)
    assert caught.value.winerror == 5
    assert len(calls) == 1
    assert target.read_bytes() == b"old document"
