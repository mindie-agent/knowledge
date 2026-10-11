"""Authority transitions operate on real frozen batches, without external writes."""
from contextlib import closing

import pytest

from conftest import write_settings
from mindie_knowledge.community.common import Deadline
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store


def tick(engine, monkeypatch):
    """Run one scheduler opportunity without a wall-clock performance assertion."""
    engine.stop.clear()
    monkeypatch.setattr(engine.stop, "wait", lambda _: engine.stop.set())
    try:
        engine._outbox_loop()
    finally:
        engine.stop.clear()


def prepare(store, settings, content):
    doc = store.create_draft(kind="experience", title="Synthetic public observation",
                             summary="Unverified test material.", content=content,
                             generation=settings.generation, owner="a" * 64)
    batch_id, *_ = build_batch(store, settings=settings)
    return doc, batch_id


def publisher(engine):
    sent = []

    def submit(batch, settings, state_dir, *, cancel, authority_guard):
        authority_guard()
        # Use the publisher's actual cancellation gate, not only a flag assertion.
        Deadline(cancel=cancel).step("synthetic write")
        sent.append(batch)
        return dict(status="submitted", head_sha="a" * 40,
                    pr_url="https://example.invalid/pull/1")

    engine.community = dict(submit_batch=submit,
                            reconcile_batch=lambda *a: dict(status="unknown"))
    return sent


@pytest.mark.parametrize("disabled_start", [True, False])
def test_reenable_sends_only_newly_authorized_material(tmp_path, monkeypatch, disabled_start):
    path = tmp_path / "sharing.json"
    first = write_settings(path, roots=[tmp_path])
    with closing(Store(tmp_path / "store", "test")) as store:
        if disabled_start:
            old_doc, old_batch = prepare(store, first, "Prior authorization material.")
            write_settings(path, enabled=False, roots=[tmp_path])
        engine = Engine(store, settings_path=path)
        engine.revoke_stale()  # The startup gate, before workers begin.
        if not disabled_start:
            # Observe enabled A before an unsent batch exists, then disable.
            tick(engine, monkeypatch)
            old_doc, old_batch = prepare(store, first, "Prior authorization material.")
            write_settings(path, enabled=False, roots=[tmp_path])
            tick(engine, monkeypatch)
        revoked_token = engine._cancel
        current = write_settings(path, roots=[tmp_path, tmp_path / "additional-project"])
        new_doc, new_batch = prepare(store, current, "Subsequent authorization material.")
        sent = publisher(engine)
        tick(engine, monkeypatch)
        assert len(sent) == 1
        assert sent[0]["entry_refs"] == [store.ref(new_doc["entry_id"], new_doc["revision"])]
        assert store.batch(new_batch)["status"] == "submitted"
        assert store.batch(old_batch)["status"] == "disabled"
        assert "Prior authorization material." in store.get(store.ref(old_doc["entry_id"]))["content"]
        assert revoked_token.is_set()  # A new generation must not revive its old token.


def test_changed_generation_keeps_material_admitted_before_poll(tmp_path, monkeypatch):
    path = tmp_path / "sharing.json"
    first = write_settings(path, roots=[tmp_path])
    with closing(Store(tmp_path / "store", "test")) as store:
        engine = Engine(store, settings_path=path)
        tick(engine, monkeypatch)
        old_doc, old_batch = prepare(store, first, "Old generation material.")
        retired_token = engine._cancel
        # Stop intake can admit the new generation before the outbox notices it.
        current = write_settings(path, roots=[tmp_path, tmp_path / "additional-project"])
        new_doc, new_batch = prepare(store, current, "Already admitted under the new generation.")
        # The summary worker also reads current authority independently of this poll.
        current_summary = engine._begin_summary(current.generation)
        for doc, generation in [(old_doc, first.generation), (new_doc, current.generation)]:
            store.record_vote(root_hash="b" * 64, ref=store.ref(doc['entry_id'], doc['revision']),
                              rating="up", reason="", publishable=True, generation=generation)
        sent = publisher(engine)
        tick(engine, monkeypatch)
        assert len(sent) == 1
        assert sent[0]["entry_refs"] == [store.ref(new_doc["entry_id"], new_doc["revision"])]
        assert store.batch(new_batch)["status"] == "submitted"
        assert store.batch(old_batch)["status"] == "disabled"
        assert retired_token.is_set()
        assert not current_summary.is_set()
        assert store.unbatched_votes(generation=first.generation) == []
        assert len(store.unbatched_votes(generation=current.generation)) == 1


def test_authority_fault_parks_batch_and_restored_generation_reuses_it(tmp_path, monkeypatch):
    path = tmp_path / "sharing.json"
    settings = write_settings(path, roots=[tmp_path])
    original = path.read_bytes()
    with closing(Store(tmp_path / "store", "test")) as store:
        engine = Engine(store, settings_path=path)
        tick(engine, monkeypatch)
        token = engine._cancel
        _, batch_id = prepare(store, settings, "Material parked while authority is unreadable.")
        sent = publisher(engine)
        path.write_bytes(b"{broken authority")
        tick(engine, monkeypatch)
        assert not sent and store.batch(batch_id)["status"] == "pending"
        path.write_bytes(original)
        tick(engine, monkeypatch)
        assert len(sent) == 1 and store.batch(batch_id)["status"] == "submitted"
        assert engine._cancel is token and not token.is_set()


def test_generation_change_preserves_unknown_write_for_read_only_reconciliation(tmp_path, monkeypatch):
    path = tmp_path / "sharing.json"
    first = write_settings(path, roots=[tmp_path])
    with closing(Store(tmp_path / "store", "test")) as store:
        engine = Engine(store, settings_path=path)
        tick(engine, monkeypatch)
        _, uncertain = prepare(store, first, "Original uncertain remote intent.")
        store.mark_batch(uncertain, "unknown", attempted=True, detail="push outcome unknown")
        current = write_settings(path, roots=[tmp_path, tmp_path / "additional-project"])
        _, new_batch = prepare(store, current, "Independent current generation material.")
        sent = publisher(engine)
        reconciled = []
        engine.community["reconcile_batch"] = lambda batch_id, *a: (
            reconciled.append(batch_id) or dict(status="unknown", detail="no remote proof"))
        tick(engine, monkeypatch)
        assert reconciled == [uncertain]
        assert store.batch(uncertain)["status"] == "unknown"
        assert [batch["batch_id"] for batch in sent] == [new_batch]


def test_summary_cancellation_belongs_to_its_generation(tmp_path):
    with closing(Store(tmp_path / "store", "test")) as store:
        engine = Engine(store)
        prior = engine._begin_summary("prior")
        engine._cancel_unsent("authority changed", keep_generation="current")
        current = engine._begin_summary("current")
        assert prior.is_set() and not current.is_set()
        engine._cancel_unsent("delayed authority observation", keep_generation="current")
        assert prior.is_set() and not current.is_set()
        engine._cancel_unsent("sharing disabled")
        assert current.is_set()


@pytest.mark.parametrize("disabled_start", [False, True])
def test_revocation_cancels_work_even_before_outbox_observes_enable(tmp_path, monkeypatch, disabled_start):
    path = tmp_path / "sharing.json"
    write_settings(path, enabled=not disabled_start, roots=[tmp_path])
    with closing(Store(tmp_path / "store", "test")) as store:
        engine = Engine(store, settings_path=path)
        engine.revoke_stale()
        # The summary worker can observe enable independently, before the
        # outbox's first enabled tick (or its next poll after disabled startup).
        current = write_settings(path, roots=[tmp_path])
        _, batch_id = prepare(store, current, "Material admitted before the first enabled outbox poll.")
        active = engine._begin_summary(current.generation)
        assert engine._generation is None
        write_settings(path, enabled=False, roots=[tmp_path])
        tick(engine, monkeypatch)
        assert active.is_set()
        assert store.batch(batch_id)["status"] == "disabled"
