"""The consent authority's reads must never make a writer's atomic replace fail.

On Windows a stdlib ``open`` shares read/write but denies delete, so
``os.replace`` (MoveFileEx) fails with ERROR_SHARING_VIOLATION whenever a
reader holds the destination open at that instant. The store's read path
opens with FILE_SHARE_DELETE there. This check is cross-platform by
construction: on POSIX the replace tolerates any open reader anyway; on
Windows it exercises the shared-delete branch for real.
"""

import os

from mindie_knowledge import consent_store


def test_replace_succeeds_while_a_reader_holds_the_file(tmp_path):
    target = tmp_path / "consent.json"
    target.write_text('{"schema":"mindie-consent/1","choice":"later"}\n')
    if os.name == "nt":
        reader = consent_store._open_for_read(target)
    else:
        reader = open(target, "rb")
    try:
        fresh = tmp_path / "fresh.json"
        fresh.write_text('{"schema":"mindie-consent/1","choice":"contribute"}\n')
        os.replace(fresh, target)  # must not fail against the open reader
    finally:
        reader.close()
    assert b"contribute" in target.read_bytes()


def test_record_choice_completes_while_a_reader_holds_the_file(tmp_path):
    target = tmp_path / "consent.json"
    consent_store.record_choice(target, "later")
    if os.name == "nt":
        reader = consent_store._open_for_read(target)
    else:
        reader = open(target, "rb")
    try:
        result = consent_store.record_choice(target, "contribute")
    finally:
        reader.close()
    assert result["state"] == "ok" and result["choice"] == "contribute"


def test_read_maps_missing_through_the_same_branch(tmp_path):
    state, data = consent_store._read_raw(tmp_path / "missing.json")
    assert state == "missing" and data is None
    found = consent_store.read(tmp_path / "missing.json")
    assert found["state"] == "missing" and found["error"] is None
