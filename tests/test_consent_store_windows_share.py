"""Publication while a product read handle is open.

``FILE_SHARE_DELETE`` on the reader is required and not sufficient. CPython
documents that ``os.replace`` / ``MoveFileExW`` still rejects an open
destination (https://github.com/python/cpython/issues/90161). These tests
call the product writers only. They do not assert that ``os.replace``
succeeds against an open Windows file.
"""

import json

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
