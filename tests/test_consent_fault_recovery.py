"""Consent faults are not revocations of work already received.

A missing, unreadable, corrupt, or invalid authority must stop new reads,
model calls, and outbound submits, but it must leave already admitted work
and saved results locatable. The same rule covers a present but unusable
consent_config, a non-boolean enabled value, and a consent document that
has reporting but no choice. After the same authority is readable and
contribute again, that work continues without a new choice. Explicit
read-only / later / disabled, and a boolean enabled=false, still revoke.
"""

import json
import sys
import time

import pytest

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.budget import MaintenanceBudget
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, canonical, new_identity

from conftest import make_admission, write_settings


TOKEN = "FAULTCTRL already received"


@pytest.fixture
def consent_api():
    try:
        from mindie_knowledge import consent_store
    except ImportError as exc:
        pytest.fail(f"candidate consent_store is not importable ({exc})")
    return consent_store


def _runner(path, marker):
    path.write_text(
        "import pathlib, json, sys\n"
        f"mark = pathlib.Path({str(marker)!r})\n"
        "mark.write_text((mark.read_text() if mark.exists() else '') + 'x\\n')\n"
        "payload = json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
        "text = payload.get('increment', '')[:400]\n"
        "sys.stdout.buffer.write(json.dumps({'entries':[{'entry_id':None,'title':'Fault',"
        "'summary':'consent fault recovery','content':text,'conditions':{}}]}).encode('utf-8'))\n"
    )


def _world(tmp_path, consent_api):
    project = tmp_path / "proj"
    project.mkdir()
    consent = tmp_path / "mindie-consent.json"
    settings = tmp_path / "community.json"
    write_settings(
        settings, enabled=True, roots=[project], consent_config=str(consent.resolve())
    )
    consent_api.record_choice(consent, "contribute")
    consent_api.record_reporting(consent, "later")
    admission = make_admission(tmp_path, project_root=project, session="ses-fault")
    store = Store(tmp_path / "store", "test")
    marker = tmp_path / "spawns"
    runner = tmp_path / "runner.py"
    _runner(runner, marker)
    engine = Engine(
        store,
        agent_command=[sys.executable, str(runner)],
        settings_path=settings,
        admission=Admission(admission),
    )
    return {
        "store": store,
        "engine": engine,
        "settings": settings,
        "consent": consent,
        "marker": marker,
        "project": project,
        "saved": consent.read_bytes(),
        "settings_bytes": settings.read_bytes(),
        "generation": json.loads(settings.read_text())["generation"],
    }


def _spawns(world):
    marker = world["marker"]
    return marker.read_text().count("x") if marker.exists() else 0


def _docs(store):
    with store.lock:
        return [row[0] for row in store.db.execute("SELECT doc FROM entries")]


def _release_due(store, capture_id):
    """Advance an already-eligible backoff to now. Does not revive eligible=0."""
    store.db.execute(
        "UPDATE continuations SET due=0 WHERE capture_id=? AND eligible!=0",
        (capture_id,),
    )
    store.db.commit()


def _drain(engine, store, limit=6):
    seen = []
    for _ in range(limit):
        ident = store.due_capture()
        if ident:
            seen.append(ident)
            engine._process(ident)
            continue
        if store.due_application():
            engine._apply_due()
            seen.append("apply")
            continue
        break
    return seen


def _corrupt(world):
    world["consent"].write_text("{not-json\n")


def _restore(world):
    world["consent"].write_bytes(world["saved"])


def _patch_settings(world, **fields):
    data = json.loads(world["settings"].read_text())
    data.update(fields)
    world["settings"].write_text(json.dumps(data, indent=2) + "\n")
    return json.loads(world["settings"].read_text())


_KEEP = {"queued", "pending", "deferred", "apply-pending"}
_MALFORMED = (
    "null-config",
    "empty-config",
    "number-config",
    "bool-config",
    "relative-config",
    "enabled-string-false",
    "enabled-string-true",
    "enabled-zero",
    "choice-absent",
)


def _introduce(world, kind):
    """One fault on top of a legal contribute configuration. Not a new choice."""
    if kind == "null-config":
        raw = _patch_settings(world, consent_config=None)
        assert "consent_config" in raw and raw["consent_config"] is None
    elif kind == "empty-config":
        raw = _patch_settings(world, consent_config="")
        assert raw["consent_config"] == ""
    elif kind == "number-config":
        raw = _patch_settings(world, consent_config=1)
        assert type(raw["consent_config"]) is int
    elif kind == "bool-config":
        raw = _patch_settings(world, consent_config=True)
        assert type(raw["consent_config"]) is bool
    elif kind == "relative-config":
        raw = _patch_settings(world, consent_config="mindie-consent.json")
        assert raw["consent_config"] == "mindie-consent.json"
    elif kind == "enabled-string-false":
        raw = _patch_settings(world, enabled="false")
        assert type(raw["enabled"]) is str and raw["enabled"] == "false"
    elif kind == "enabled-string-true":
        raw = _patch_settings(world, enabled="true")
        assert type(raw["enabled"]) is str and raw["enabled"] == "true"
    elif kind == "enabled-zero":
        raw = _patch_settings(world, enabled=0)
        assert type(raw["enabled"]) is int
    elif kind == "choice-absent":
        world["consent"].write_text(
            json.dumps({"schema": "mindie-consent/1", "reporting": "later"}) + "\n"
        )
        saved = json.loads(world["consent"].read_text())
        assert "choice" not in saved and saved["reporting"] == "later"
    else:
        raise AssertionError(kind)


def _repair(world, kind):
    """Put the original authority bytes back. Does not record a new choice."""
    if kind == "choice-absent":
        _restore(world)
    else:
        world["settings"].write_bytes(world["settings_bytes"])


def test_corrupt_authority_keeps_admitted_work_then_resumes(tmp_path, consent_api):
    world = _world(tmp_path, consent_api)
    store, engine = world["store"], world["engine"]
    try:
        local = store.create_draft(
            kind="experience",
            title="Local",
            summary="still readable",
            content="READCTRL survives the consent fault",
        )
        captured = engine.capture(
            session_id="ses-fault", turn_id="t-fault", summary=TOKEN
        )
        assert captured["status"] == "queued", captured
        generation = json.loads(world["settings"].read_text())["generation"]
        assert generation == world["generation"]
        _corrupt(world)
        status = engine._settings()
        assert status.allows_capture() is False
        assert status.public_status()["consent"]["state"] == "corrupt"
        assert store.query("READCTRL")["results"]
        assert local["entry_id"]
        _drain(engine, store)
        row = store.capture_row(captured["id"])
        assert row["status"] in {"queued", "pending", "deferred", "apply-pending"}, {
            "status": row["status"],
            "detail": row["detail"],
        }
        assert row["transcript"] == captured.get("transcript") or row["summary"]
        assert TOKEN in (row["summary"] or "")
        assert _spawns(world) == 0
        assert json.loads(world["settings"].read_text())["generation"] == generation
        _restore(world)
        assert engine._settings().allows_capture() is True
        assert engine._settings().public_status()["consent"]["choice"] == "contribute"
        _release_due(store, captured["id"])
        _drain(engine, store)
        row = store.capture_row(captured["id"])
        assert row["status"] == "organized", row
        assert TOKEN in "\n".join(_docs(store))
        assert _spawns(world) == 1
        assert json.loads(world["settings"].read_text())["generation"] == generation
        assert store.query("READCTRL")["results"]
    finally:
        store.close()


@pytest.mark.parametrize("choice", ["read-only", "later", "disabled"])
def test_explicit_non_contribute_still_revokes_admitted_work(tmp_path, consent_api, choice):
    world = _world(tmp_path, consent_api)
    store, engine = world["store"], world["engine"]
    try:
        captured = engine.capture(
            session_id="ses-fault", turn_id=f"t-{choice}", summary=TOKEN
        )
        assert captured["status"] == "queued", captured
        consent_api.record_choice(world["consent"], choice)
        _drain(engine, store)
        row = store.capture_row(captured["id"])
        assert row["status"] == "cancelled", row
        assert _spawns(world) == 0
        assert TOKEN not in "\n".join(_docs(store))
        # Restoring contribute afterwards is a new explicit choice, not a
        # repair of an unreadable file, and it does not resurrect the revoke.
        consent_api.record_choice(world["consent"], "contribute")
        assert engine._settings().allows_capture() is True
        _release_due(store, captured["id"])
        _drain(engine, store)
        assert store.capture_row(captured["id"])["status"] == "cancelled"
        assert _spawns(world) == 0
    finally:
        store.close()


@pytest.mark.parametrize("kind", _MALFORMED)
def test_malformed_authority_keeps_admitted_work_then_resumes(tmp_path, consent_api, kind):
    world = _world(tmp_path, consent_api)
    store, engine = world["store"], world["engine"]
    try:
        captured = engine.capture(
            session_id="ses-fault", turn_id=f"t-{kind}", summary=TOKEN
        )
        assert captured["status"] == "queued", captured
        generation = world["generation"]
        _introduce(world, kind)
        _drain(engine, store)
        row = store.capture_row(captured["id"])
        observed = {
            "allows_capture": engine._settings().allows_capture(),
            "status": row["status"],
            "detail": row["detail"],
            "spawns": _spawns(world),
        }
        assert [
            observed["allows_capture"],
            observed["status"] in _KEEP,
            observed["spawns"],
        ] == [False, True, 0], observed
        assert TOKEN in (row["summary"] or "")
        assert json.loads(world["settings"].read_text())["generation"] == generation
        _repair(world, kind)
        assert engine._settings().allows_capture() is True
        assert engine._settings().public_status()["consent"]["choice"] == "contribute"
        _release_due(store, captured["id"])
        _drain(engine, store)
        row = store.capture_row(captured["id"])
        assert row["status"] == "organized", row
        assert TOKEN in "\n".join(_docs(store))
        assert _spawns(world) == 1
        assert json.loads(world["settings"].read_text())["generation"] == generation
    finally:
        store.close()


def _stage_saved(world):
    store, engine = world["store"], world["engine"]
    calls = []

    def submit_batch(*args, **kwargs):
        calls.append(args)
        return {"status": "submitted", "detail": "recorder"}

    engine.community = {
        "submit_batch": submit_batch,
        "reconcile_batch": lambda *args, **kwargs: {"status": "unknown"},
    }
    captured = engine.capture(
        session_id="ses-fault",
        turn_id="t-saved",
        summary="body comes from the saved result",
    )
    attempt = f"organize:{captured['id']}:fault"
    result = {
        "entries": [
            {
                "entry_id": new_identity(),
                "new": True,
                "title": "Saved",
                "summary": "reused after the fault",
                "content": "SAVEDFAULT reused after consent repair",
                "conditions": {},
            }
        ]
    }
    budget = MaintenanceBudget(store)
    budget.reserve(attempt, captured["id"], "organize")
    budget.checkpoint(
        attempt,
        canonical(result),
        canonical({"items": [], "notes": [], "refs": [], "more": False}),
        capture_id=captured["id"],
        capture_status="apply-pending",
    )
    with store._write_txn():
        store.db.execute(
            "INSERT INTO outbox(batch_id, revision, batch, status, detail, "
            "pr_url, head_sha, created, attempted, updated, reconciliations, "
            "next_attempt, generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "batch-fault",
                "rev-1",
                json.dumps({"entry_refs": []}),
                "pending",
                "",
                None,
                None,
                time.time(),
                None,
                time.time(),
                0,
                None,
                world["generation"],
            ),
        )
    return attempt, calls, budget


def _press_saved(world):
    store, engine = world["store"], world["engine"]
    engine.revoke_stale()
    engine._apply_due()
    engine._submit(store.batch("batch-fault"))


def _saved_observed(world, attempt, calls, budget):
    store = world["store"]
    saved = budget.application(attempt)
    batch = store.batch("batch-fault")
    return {
        "batch": None if batch is None else batch["status"],
        "capture": store.capture_row(attempt.split(":")[1])["status"],
        "result_kept": bool(saved and saved.get("result") and "SAVEDFAULT" in saved["result"]),
        "submits": len(calls),
        "spawns": _spawns(world),
    }


def _resume_saved(world, attempt):
    store, engine = world["store"], world["engine"]
    capture_id = attempt.split(":")[1]
    _release_due(store, capture_id)
    engine._apply_due()
    assert store.capture_row(capture_id)["status"] == "organized"
    assert "\n".join(_docs(store)).count("SAVEDFAULT") == 1
    assert _spawns(world) == 0
    engine._submit(store.batch("batch-fault"))


def test_corrupt_authority_keeps_a_saved_result_and_pending_submit(tmp_path, consent_api):
    world = _world(tmp_path, consent_api)
    store = world["store"]
    try:
        attempt, calls, budget = _stage_saved(world)
        _corrupt(world)
        _press_saved(world)
        observed = _saved_observed(world, attempt, calls, budget)
        assert observed == {
            "batch": "pending",
            "capture": "apply-pending",
            "result_kept": True,
            "submits": 0,
            "spawns": 0,
        }, observed
        _restore(world)
        _resume_saved(world, attempt)
        assert len(calls) == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "kind",
    ("null-config", "enabled-string-false", "choice-absent"),
)
def test_malformed_authority_keeps_a_saved_result_and_pending_submit(
    tmp_path, consent_api, kind
):
    world = _world(tmp_path, consent_api)
    store = world["store"]
    try:
        attempt, calls, budget = _stage_saved(world)
        _introduce(world, kind)
        _press_saved(world)
        observed = _saved_observed(world, attempt, calls, budget)
        assert observed == {
            "batch": "pending",
            "capture": "apply-pending",
            "result_kept": True,
            "submits": 0,
            "spawns": 0,
        }, observed
        _repair(world, kind)
        assert world["engine"]._settings().allows_capture() is True
        _resume_saved(world, attempt)
        assert len(calls) == 1
        assert json.loads(world["settings"].read_text())["generation"] == world["generation"]
    finally:
        store.close()


def test_explicit_boolean_false_still_revokes_admitted_work(tmp_path, consent_api):
    world = _world(tmp_path, consent_api)
    store, engine = world["store"], world["engine"]
    try:
        captured = engine.capture(
            session_id="ses-fault", turn_id="t-enabled-false", summary=TOKEN
        )
        assert captured["status"] == "queued", captured
        raw = _patch_settings(world, enabled=False)
        assert type(raw["enabled"]) is bool and raw["enabled"] is False
        _drain(engine, store)
        row = store.capture_row(captured["id"])
        assert row["status"] == "cancelled", row
        assert _spawns(world) == 0
        assert TOKEN not in "\n".join(_docs(store))
        world["settings"].write_bytes(world["settings_bytes"])
        assert engine._settings().allows_capture() is True
        _release_due(store, captured["id"])
        _drain(engine, store)
        assert store.capture_row(captured["id"])["status"] == "cancelled"
        assert _spawns(world) == 0
    finally:
        store.close()
