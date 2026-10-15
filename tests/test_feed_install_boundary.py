"""One untrusted package validation boundary, with atomic prior visibility."""
from contextlib import closing

import pytest

from mindie_knowledge.loop.documents import make_entry
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store
from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.materials.file_source import FileContents
import mindie_knowledge.materials.store as material_module
from package_fixture import package_for, write_package


def package(ident, title):
    return package_for(make_entry(
        entry_id=ident * 64, domain="vllm-ascend", kind="experience",
        title=title, summary=title, content="Complete evidence: " + title,
    ))


@pytest.fixture
def installed(tmp_path):
    with closing(Store(tmp_path / "consumer", "vllm-ascend")) as store:
        previous = package("a", "Previously installed")
        store.install_feed([previous], feed_ident="f" * 64, source_revision="1" * 40)
        yield store


def snapshot(store):
    return (
        (store.materials.root / "current.json").read_bytes(),
        [tuple(row) for row in store.db.execute("SELECT * FROM entries ORDER BY entry_id")],
        store.feed_get("published-commit"),
    )


def test_streamed_feed_validates_each_package_only_at_installation(installed, monkeypatch):
    feed = Feed(installed, dict(domain="vllm-ascend", repository="org/knowledge"))
    staging = feed._staging_dir("2" * 40)
    candidate = package("b", "Next snapshot")
    write_package(staging, candidate)
    entries = [(f"tasks/{candidate['task_id']}/{name}", 0, "")
               for name in candidate["files"]]
    checked = []
    original = material_module.validate_package_files

    def validate(files, domain):
        value = original(files, domain)
        checked.append(value["task_id"])
        return value

    monkeypatch.setattr(material_module, "validate_package_files", validate)
    result = installed.install_feed(feed._packages("2" * 40, entries),
                                    feed_ident=feed.ident, source_revision="2" * 40)
    assert result == {"entries": 1}
    assert checked == [candidate["task_id"]]
    assert installed.materials.get_document(candidate["task_id"], source="feed")["content"] == (
        "Complete evidence: Next snapshot"
    )


@pytest.mark.parametrize("failure", ["late_bad_body", "wrong_identity", "duplicate", "unfinished"])
def test_rejected_package_keeps_prior_metadata_and_pointers(installed, tmp_path, failure):
    before = snapshot(installed)
    candidate = package("b", "Candidate")
    rejected = package("c", "Rejected")
    if failure == "late_bad_body":
        path = next(path for path in rejected["files"] if path.startswith("blocks/"))
        rejected["files"][path] += "tampered"
        error = "hash mismatch"
    elif failure == "wrong_identity":
        rejected["task_id"] = "d" * 64
        error = "envelope identity"
    elif failure == "duplicate":
        rejected = candidate
        error = "duplicate task"
    else:
        with closing(MaterialStore(tmp_path / "unfinished", "vllm-ascend")) as author:
            author.append_batch(
                "c" * 64,
                [dict(block_id="e" * 64, text="Unindexed body.", source_range={},
                      title="", summary="")],
                "Pending index.", title="Unfinished", promote=True,
            )
            rejected = author.export_task("c" * 64)
        error = "unfinished material indexes"
    with pytest.raises(ValueError, match=error):
        installed.install_feed(iter([candidate, rejected]), feed_ident="f" * 64,
                               source_revision="2" * 40)
    assert snapshot(installed) == before


def test_callback_rejection_is_before_file_write_or_pointer_promotion(installed):
    before = snapshot(installed)
    candidate = package("b", "Rejected by metadata owner")

    def reject(checked):
        assert checked["entry"] == candidate["entry"]
        raise ValueError("metadata owner rejected candidate")

    with pytest.raises(ValueError, match="metadata owner rejected"):
        installed.materials.install_packages([candidate], on_validated=reject)
    assert snapshot(installed) == before
    assert not (installed.materials.root / "tasks" / candidate["task_id"]).exists()


def test_source_change_after_validation_is_rejected_during_copy(installed, tmp_path):
    before = snapshot(installed)
    candidate = package("b", "Source can change")
    write_package(tmp_path / "source", candidate)
    root = tmp_path / "source" / "tasks" / candidate["task_id"]
    files = FileContents.from_paths(root, candidate["files"])
    path = next(path for path in candidate["files"] if path.startswith("blocks/"))

    def change_source(_checked):
        (root / path).write_text("Changed after complete validation.", encoding="utf-8")

    with pytest.raises(ValueError, match="identity mismatch"):
        installed.materials.install_packages(
            [dict(task_id=candidate["task_id"], files=files)], on_validated=change_source,
        )
    assert snapshot(installed) == before


def test_file_membership_never_reads_body_but_content_still_validates(tmp_path):
    path = tmp_path / "index.md"
    path.write_text("Original manifest", encoding="utf-8")
    files = FileContents.from_paths(tmp_path, ["index.md"])
    path.write_text("Changed manifest", encoding="utf-8")
    assert "index.md" in files
    assert "missing.md" not in files
    with pytest.raises(ValueError, match="identity mismatch"):
        files["index.md"]
