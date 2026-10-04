"""Orthogonalized cases derived from the four long K3 task shapes.

No user transcript, host, path, model response or business value is included.
The source observations are long append-only runs, misleading intermediate
benchmarks followed by correction, unresolved hypotheses, and repeat progress.
"""
from contextlib import ExitStack, closing
import hashlib
from pathlib import Path

import pytest

from mindie_knowledge.loop import documents
from mindie_knowledge.materials import MaterialStore, validate_package_files
from mindie_knowledge.materials.store import MaterialCleanupError, _split_text


@pytest.fixture
def material_store(tmp_path):
    """Own every test store, including catalogs reopened after an explicit close."""
    with ExitStack() as owners:
        def create(root, domain):
            return owners.enter_context(closing(MaterialStore(root, domain)))
        yield create


TASK = "a" * 64


def block(number, text, **kwargs):
    return dict(block_id=hashlib.sha256(str(number).encode()).hexdigest(), text=text,
                source_range={"start": number, "end": number + 1}, title="", summary="", **kwargs)


def append(store, blocks, **kwargs):
    task = store.append_batch(TASK, blocks, "Work is incomplete; hypotheses remain unverified.",
                              title="Long inference task", domain="demo", **kwargs)
    store.retain_current(TASK, {"draft": task["entry"]["revision"]})
    return task


def test_append_preserves_complete_unicode_and_never_reads_prior_body(tmp_path, monkeypatch, material_store):
    store = material_store(tmp_path, "demo")
    first = append(store, [block(0, "  early\n\n中段证据\n")])
    old_path = store.root / "tasks" / TASK / "blocks" / (first["blocks"][0]["block_id"] + ".md")
    original = Path.read_bytes

    def guarded(path, *args, **kwargs):
        if path == old_path:
            raise AssertionError("append reread an earlier material body")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    task = append(store, [block(1, "late correction\n\n")])
    assert len(task["blocks"]) == 2
    monkeypatch.setattr(Path, "read_bytes", original)
    expected = "  early\n\n中段证据\nlate correction\n\n"
    assert store.get_document(TASK)["content"] == expected
    package = store.export_task(TASK)
    consumer = material_store(tmp_path / "consumer", "demo")
    consumer.install_packages([package])
    assert consumer.get_document(TASK)["content"].encode("utf-8") == expected.encode("utf-8")
    assert consumer.export_task(TASK, source="feed")["files"] == package["files"]


def test_index_headers_change_manifest_without_touching_body(tmp_path, monkeypatch, material_store):
    store = material_store(tmp_path, "demo")
    task = append(store, [block(0, "A hypothesis, not a validated result.\n")])
    original = Path.read_bytes

    def no_blocks(path, *args, **kwargs):
        if path.parent.name == "blocks":
            raise AssertionError("index update read material body")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", no_blocks)
    updated = store.update_indexes(TASK, [dict(block_id=task["blocks"][0]["block_id"],
                                               title="Unresolved hypothesis", summary="No acceptance yet.")],
                                   "The proposed cause remains unverified.")
    store.retain_current(TASK, {"draft": updated["entry"]["revision"]})
    assert updated["blocks"][0]["indexed"]
    assert updated["entry"]["revision"] != task["entry"]["revision"]
    assert updated["blocks"][0]["sha256"] == task["blocks"][0]["sha256"]


def test_completed_event_replay_does_not_downgrade_its_block_index(tmp_path, material_store):
    store = material_store(tmp_path, "demo")
    raw = block(0, "Repeated progress belongs to the same source range.")
    task = append(store, [raw])
    indexed = store.update_indexes(TASK, [dict(block_id=raw["block_id"], title="Progress", summary="Repeated event.")],
                                   "Still incomplete.", promote=True)
    replay = store.append_batch(TASK, [raw], indexed["navigation"], title=indexed["entry"]["title"])
    assert len(replay["blocks"]) == 1
    assert replay["blocks"][0]["indexed"]


def test_unicode_split_is_lossless_and_bounded():
    text = ("α中\n" * 9000) + "final correction"
    pieces = list(_split_text(text))
    assert "".join(pieces) == text
    assert max(len(part.encode()) for part in pieces) <= 16384


def test_feed_validation_is_atomic_and_independent_consumer_needs_no_model(tmp_path, material_store):
    author = material_store(tmp_path / "author", "demo")
    append(author, [block(0, "Complete middle evidence.\n")])
    package = author.export_task(TASK)
    consumer = material_store(tmp_path / "consumer", "demo")
    consumer.install_packages([package], source_revision="1" * 40)
    assert consumer.get_document(TASK, source="feed")["content"] == "Complete middle evidence.\n"
    changed = dict(package, files=dict(package["files"]))
    path = next(key for key in changed["files"] if key.startswith("blocks/"))
    changed["files"][path] += "tampered"
    before = (consumer.root / "current.json").read_bytes()
    with pytest.raises(ValueError, match="hash mismatch"):
        consumer.install_packages([changed], source_revision="2" * 40)
    assert (consumer.root / "current.json").read_bytes() == before
    assert not (consumer.root / "session").exists()
    assert consumer.search("middle evidence")[0]["source"] == "feed"
    consumer.close()


@pytest.mark.parametrize("changed_file", ["block", "manifest"])
def test_crlf_mutation_of_authoritative_file_is_rejected_before_export_or_index(tmp_path, changed_file, material_store):
    store = material_store(tmp_path, "demo")
    task = append(store, [block(0, "Complete UTF-8 evidence 中段.\n")])
    package = store.export_task(TASK)
    assert store.search("evidence")[0]["entry_id"] == TASK
    pointer = (store.root / "current.json").read_bytes()
    snapshot = store.root / ".reme-index" / "snapshot.json"
    indexed = snapshot.read_bytes()
    task_root = store.root / "tasks" / TASK
    target = (task_root / "blocks" / (task["blocks"][0]["block_id"] + ".md")
              if changed_file == "block" else
              task_root / "manifests" / (task["entry"]["revision"] + ".md"))
    original = target.read_bytes()
    changed = original.replace(b"\n", b"\r\n")
    assert hashlib.sha256(changed).digest() != hashlib.sha256(original).digest()
    target.write_bytes(changed)
    error = "material block hash mismatch" if changed_file == "block" else "canonical YAML frontmatter"
    try:
        for operation in (lambda: store.get_document(TASK), lambda: store.export_task(TASK),
                          lambda: store.search("evidence")):
            with pytest.raises(ValueError, match=error):
                operation()
            assert target.read_bytes() == changed
            assert (store.root / "current.json").read_bytes() == pointer
            assert snapshot.read_bytes() == indexed
        with pytest.raises(ValueError, match="immutable material identity conflicts"):
            store.install_packages([package], source_revision="2" * 40)
        assert (store.root / "current.json").read_bytes() == pointer
        assert snapshot.read_bytes() == indexed
        assert target.read_bytes() == changed
    finally:
        store.close()


def test_candidates_do_not_replace_current_before_metadata_commit(tmp_path, material_store):
    store = material_store(tmp_path, "demo")
    old = append(store, [block(0, "Initial claim.")])
    candidate = store.append_batch(TASK, [block(1, " Explicit correction.")], "Corrected.")
    assert store.read_task(TASK)["entry"]["revision"] == old["entry"]["revision"]
    assert store.get_document(TASK, candidate["entry"]["revision"])["content"].endswith("correction.")
    store.retain_snapshot({TASK: {"draft": candidate["entry"]["revision"]}})
    with pytest.raises(KeyError, match="expired"):
        store.get_document(TASK, old["entry"]["revision"])
    assert len(list((store.root / "tasks" / TASK / "manifests").glob("*.md"))) == 1


def test_cleanup_failure_reports_already_committed_pointer(tmp_path, monkeypatch, material_store):
    store = material_store(tmp_path, "demo")
    task = store.append_batch(TASK, [block(0, "Material.")], "Incomplete.", title="Task", domain="demo")
    monkeypatch.setattr(store, "_prune_task", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(MaterialCleanupError) as caught:
        store.retain_current(TASK, {"draft": task["entry"]["revision"]})
    assert caught.value.committed
    assert store.read_task(TASK)["entry"]["revision"] == task["entry"]["revision"]


def test_real_reme_finds_early_middle_late_and_returns_current_correction(tmp_path, material_store):
    store = material_store(tmp_path, "demo")
    task = append(store, [block(0, "Cache layout conflict appeared before the long run.\n"),
                          block(1, "Graph replay mismatched the position offsets during the middle stage.\n"),
                          block(2, "The benchmark gain was withdrawn after detecting unequal request counts.\n")])
    indexes = [dict(block_id=b["block_id"], title=t, summary=s) for b, t, s in zip(task["blocks"],
        ["Cache organization", "Graph replay position offset", "Invalid performance comparison"],
        ["Early cache layout mismatch.", "Middle-stage graph replay failure.", "Speedup remains unverified."])]
    updated = store.update_indexes(TASK, indexes, "An initial speedup claim was withdrawn; acceptance remains incomplete.")
    store.retain_current(TASK, {"draft": updated["entry"]["revision"]})
    for query, number in [("cache layout", 0), ("graph replay position", 1), ("unequal request counts", 2)]:
        hit = store.search(query)[0]
        assert hit["block_id"] == task["blocks"][number]["block_id"]
        assert "withdrawn" in hit["navigation"]
    store.close()
    restarted = material_store(tmp_path, "demo")
    assert restarted.search("position offsets")[0]["block_id"] == task["blocks"][1]["block_id"]
    restarted.close()


def test_reme_failures_are_visible_and_explicit_rebuild_recovers(tmp_path, monkeypatch, material_store):
    from mindie_knowledge.materials.reme_checkpoint import ReMeCheckpoint
    store = material_store(tmp_path, "demo")
    append(store, [block(0, "Searchable calibration failure.")])
    original = ReMeCheckpoint.commit

    def fail(_self, *_args):
        raise OSError("derived graph/chunk checkpoint write failed")

    monkeypatch.setattr(ReMeCheckpoint, "commit", fail)
    with pytest.raises(OSError, match="derived graph/chunk checkpoint write failed"):
        store.search("calibration")
    monkeypatch.setattr(ReMeCheckpoint, "commit", original)
    store.rebuild_index()
    assert store.search("calibration")[0]["entry_id"] == TASK
    store.close()


def test_package_rejects_unreferenced_and_traversal_files(tmp_path, material_store):
    store = material_store(tmp_path, "demo")
    append(store, [block(0, "Material.")])
    files = store.export_task(TASK)["files"]
    files["../../outside.md"] = "untrusted"
    with pytest.raises(ValueError, match="exact manifest"):
        validate_package_files(files, "demo")


def test_restart_recovery_retires_an_uncommitted_new_task(tmp_path, material_store):
    store = material_store(tmp_path, "demo")
    candidate = store.append_batch(TASK, [block(0, "Candidate before process interruption.")],
                                   "Incomplete.", title="Task")
    assert store.get_document(TASK, candidate["entry"]["revision"])
    store.close()
    reopened = material_store(tmp_path, "demo")
    reopened.recover_snapshot({})
    assert not (tmp_path / "tasks" / TASK).exists()
    assert reopened.visible_tasks() == {}


def test_index_readiness_requires_reme_work_not_only_current_files(tmp_path, material_store):
    store = material_store(tmp_path, "demo")
    append(store, [block(0, "Cache layout conflict.")])
    assert store.index_status()["phase"] == "unbuilt"
    assert store.refresh_index()["complete"] is True
    store.close()
    assert store.index_status()["phase"] == "checkpointed-unverified"


def test_append_does_not_treat_missing_current_manifest_as_new_task(tmp_path, material_store):
    store = material_store(tmp_path, 'demo')
    current = append(store, [block(0, 'Preserve this earlier body.')])
    pointer = (store.root / 'current.json').read_bytes()
    manifest = store.root / 'tasks' / TASK / 'manifests' / (current['entry']['revision'] + '.md')
    manifest.unlink()
    with pytest.raises(KeyError, match='missing material'):
        append(store, [block(1, 'A later observation.')])
    assert (store.root / 'current.json').read_bytes() == pointer
    assert len(list((store.root / 'tasks' / TASK / 'blocks').glob('*.md'))) == 1


def test_cleanup_refuses_symlinked_block_directory(tmp_path, material_store):
    store = material_store(tmp_path / 'store', 'demo')
    outside = tmp_path / 'outside'
    outside.mkdir()
    protected = outside / ('b' * 64 + '.md')
    protected.write_text('Unrelated file must survive cleanup.')
    root = store.root / 'tasks' / TASK
    root.mkdir(parents=True)
    (root / 'blocks').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        store.append_batch(TASK, [block(0, 'Material.')], 'Incomplete.', title='Task')
    with pytest.raises(MaterialCleanupError) as caught:
        store.recover_snapshot({})
    assert caught.value.committed
    assert protected.read_text() == 'Unrelated file must survive cleanup.'
