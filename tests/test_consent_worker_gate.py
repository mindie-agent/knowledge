"""Consent must gate the real Stop/worker and the outbound write.

community.enabled=true is not enough once consent_config names the profile
authority. Missing, unreadable, corrupt, and non-contribute choices stop
transcript reads, model spawns, and submission. enabled=false still wins.
A present but null, empty, relative, or non-string consent_config is a
fault, not a legacy file. A non-boolean enabled value is a fault, not an
explicit off switch. A consent document with only reporting is undetermined,
not read-only/later/disabled. Reporting does not overwrite a contribute
choice. Local retrieval stays available. The production Kimi parser is the
reader; the model is a subprocess counter.
"""

import json
import sys
import time

import pytest

from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store

from conftest import make_admission, write_settings
from material_worker_fixture import command as summary_command
from mindie_knowledge.loop.transcript_redaction import install_scanner
from mindie_knowledge.loop.transcript_capture import summarize_due
from lane_support import (
    SESSIONS,
    allow_read,
    deny_read,
    entry_documents,
    load_parser,
    transcript_path,
    write_transcript,
)

PARSER_NAME = "kimi"
SESSION = SESSIONS[PARSER_NAME]
TOKEN = "GATECTRL mapping observed"


def _observe(parser):
    calls = {"n": 0}
    real = parser.read_material

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    parser.read_material = wrapped
    parser._grok_core_calls = calls
    return parser


_SHAPES = (
    "null-field",
    "empty-field",
    "number-field",
    "bool-field",
    "enabled-string-false",
    "enabled-string-true",
    "enabled-zero",
    "choice-absent",
)


def _patch_json(path, **fields):
    data = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


def _deform(world, shape):
    """Turn one legal contribute config into a single malformed gate shape."""
    if shape == "null-field":
        raw = _patch_json(world["settings"], consent_config=None)
        assert "consent_config" in raw and raw["consent_config"] is None
    elif shape == "empty-field":
        raw = _patch_json(world["settings"], consent_config="")
        assert raw["consent_config"] == ""
    elif shape == "number-field":
        raw = _patch_json(world["settings"], consent_config=1)
        assert type(raw["consent_config"]) is int
    elif shape == "bool-field":
        raw = _patch_json(world["settings"], consent_config=True)
        assert type(raw["consent_config"]) is bool
    elif shape == "enabled-string-false":
        raw = _patch_json(world["settings"], enabled="false")
        assert type(raw["enabled"]) is str and raw["enabled"] == "false"
    elif shape == "enabled-string-true":
        raw = _patch_json(world["settings"], enabled="true")
        assert type(raw["enabled"]) is str and raw["enabled"] == "true"
    elif shape == "enabled-zero":
        raw = _patch_json(world["settings"], enabled=0)
        assert type(raw["enabled"]) is int and raw["enabled"] == 0
    elif shape == "choice-absent":
        world["consent_path"].write_text(
            json.dumps({"schema": "mindie-consent/1", "reporting": "later"}) + "\n"
        )
        saved = json.loads(world["consent_path"].read_text())
        assert "choice" not in saved and saved["reporting"] == "later"
    else:
        raise AssertionError(shape)


def _world(tmp_path, *, enabled, consent):
    project = tmp_path / "proj"
    project.mkdir()
    consent_path = None
    extensions = {}
    shape = consent if consent in _SHAPES else None
    if shape:
        consent = ("contribute", "later")
        enabled = True
    if consent is not None:
        consent_path = tmp_path / "mindie-consent.json"
        if consent == "missing":
            pass
        elif consent == "corrupt":
            consent_path.write_text("{not json\n")
        elif consent == "unreadable":
            consent_path = tmp_path / "mindie-consent.json"
            consent_path.write_text(
                json.dumps(
                    {"schema": "mindie-consent/1", "choice": "contribute", "reporting": "later"}
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            deny_read(consent_path)
            try:
                consent_path.read_bytes()
            except PermissionError:
                pass
            else:
                raise AssertionError("unreadable consent fixture is still readable")
            extensions["consent_config"] = str(consent_path.resolve())
        elif consent == "relative":
            consent_path.write_text(
                json.dumps(
                    {"schema": "mindie-consent/1", "choice": "contribute", "reporting": "later"}
                )
                + "\n"
            )
            extensions["consent_config"] = "mindie-consent.json"
        else:
            choice, reporting = consent
            consent_path.write_text(
                json.dumps(
                    {
                        "schema": "mindie-consent/1",
                        "choice": choice,
                        "reporting": reporting,
                        "choice_at": time.time(),
                        "reporting_at": time.time(),
                    }
                )
                + "\n"
            )
        if "consent_config" not in extensions:
            extensions["consent_config"] = str(consent_path.resolve())
    settings = tmp_path / "community.json"
    parsed = write_settings(settings, enabled=enabled, roots=[project], **extensions)
    admission = make_admission(tmp_path, project_root=project, session=SESSION)
    log = transcript_path(PARSER_NAME, tmp_path / "logs", SESSION)
    store = Store(tmp_path / "store", "test")
    marker = tmp_path / "spawns"
    parser = _observe(load_parser(PARSER_NAME))
    engine = Engine(
        store,
        summary_command=summary_command(calls=marker),
        redactor_executable=install_scanner(),
        settings_path=settings,
        admission=Admission(admission),
        transcript_adapter=parser,
    )
    when = max(parsed.enabled_at or 0, engine.admission.active_lease(SESSION)['activated_at'],
               store.capture_floor) + 1
    write_transcript(PARSER_NAME, log, SESSION, [TOKEN], when)
    world = {
        "store": store,
        "engine": engine,
        "log": log,
        "marker": marker,
        "parser": parser,
        "settings": settings,
        "consent_path": consent_path,
        "unreadable": consent_path if consent == "unreadable" else None,
    }
    if shape:
        _deform(world, shape)
    return world


def _stop(world):
    captured = world["engine"].capture(
        session_id=SESSION,
        turn_id="t1",
        transcript_path=str(world["log"]),
        summary="",
    )
    if captured.get("id") and captured.get("status") in {"queued", "pending", "deferred"}:
        world["engine"]._process(captured["id"])
        world["engine"].last_activity = 0
        summarize_due(world["engine"])
    return captured


def _cleanup(world):
    unreadable = world.get("unreadable")
    if unreadable is not None:
        allow_read(unreadable)
    world["store"].close()


def _assert_stopped(world, captured):
    reads = world["parser"]._grok_core_calls["n"]
    spawns = world["marker"].read_text().count("x") if world["marker"].exists() else 0
    row = world["store"].capture_row(captured["id"]) if captured.get("id") else None
    evidence = {
        "reads": reads,
        "model_spawns": spawns,
        "final_status": None if row is None else row["status"],
    }
    assert (reads, spawns) == (0, 0), evidence
    if row is not None:
        assert row["status"] not in {
            "organized", "processing", "apply-pending", "pending", "queued"
        }, evidence
    assert TOKEN not in "\n".join(entry_documents(world["store"])), evidence


def _assert_captured(world):
    assert world["parser"]._grok_core_calls["n"] >= 1
    assert world["marker"].exists() and "x" in world["marker"].read_text()
    assert TOKEN in "\n".join(entry_documents(world["store"]))


@pytest.mark.parametrize(
    "consent",
    ["missing", "corrupt", "unreadable", "relative", ("read-only", "later"), ("later", "later"), ("disabled", "disabled")],
    ids=["missing", "corrupt", "unreadable", "relative", "read-only", "later", "disabled"],
)
def test_non_contribute_consent_stops_read_and_model_while_enabled(tmp_path, consent):
    world = _world(tmp_path, enabled=True, consent=consent)
    try:
        captured = _stop(world)
        _assert_stopped(world, captured)
    finally:
        _cleanup(world)


def _assert_skipped(world, captured):
    reads = world["parser"]._grok_core_calls["n"]
    spawns = world["marker"].read_text().count("x") if world["marker"].exists() else 0
    evidence = {
        "status": captured.get("status"),
        "id": captured.get("id"),
        "reason": captured.get("reason"),
        "reads": reads,
        "model_spawns": spawns,
    }
    assert captured.get("status") == "skipped", evidence
    assert not captured.get("id"), evidence
    assert (reads, spawns) == (0, 0), evidence
    assert TOKEN not in "\n".join(entry_documents(world["store"])), evidence


@pytest.mark.parametrize("consent", _SHAPES)
def test_malformed_gate_skips_a_new_stop(tmp_path, consent):
    """Key absent stays legacy. A present bad value, a bad enabled type, or
    a consent file with no choice does not read or spawn."""
    world = _world(tmp_path, enabled=True, consent=consent)
    try:
        captured = _stop(world)
        _assert_skipped(world, captured)
    finally:
        _cleanup(world)


def test_enabled_false_wins_over_contribute(tmp_path):
    world = _world(tmp_path, enabled=False, consent=("contribute", "enabled"))
    try:
        captured = _stop(world)
        _assert_stopped(world, captured)
    finally:
        _cleanup(world)


def test_contribute_still_captures_with_reporting_later(tmp_path):
    """Reporting later is not a reason to refuse contribution or to re-ask."""
    world = _world(tmp_path, enabled=True, consent=("contribute", "later"))
    try:
        captured = _stop(world)
        assert captured.get("status") == "queued", captured
        _assert_captured(world)
    finally:
        _cleanup(world)


def test_old_community_file_without_consent_config_still_captures(tmp_path):
    """No consent_config is the pre-migration format, not a new denial.
    It is also not proof that an upgrade installed the authority."""
    world = _world(tmp_path, enabled=True, consent=None)
    try:
        raw = json.loads(world["settings"].read_text())
        assert "consent_config" not in raw
        captured = _stop(world)
        assert captured.get("status") == "queued", captured
        _assert_captured(world)
    finally:
        _cleanup(world)


def test_blocked_consent_keeps_local_retrieval(tmp_path):
    """Local read stays available when the consent file is corrupt.
    This does not prove the write gate; the stop tests cover that."""
    world = _world(tmp_path, enabled=True, consent="corrupt")
    try:
        document = world["store"].create_draft(
            kind="experience",
            title="Local note",
            summary="still readable",
            content="READCTRL local retrieval survives a corrupt consent file",
        )
        found = world["store"].query("READCTRL")
        assert found["results"], found
        body = world["store"].get(found["results"][0]["ref"])
        assert "READCTRL" in body["content"]
        assert document["entry_id"]
    finally:
        _cleanup(world)


def _pending_batch(store, generation):
    from mindie_knowledge.loop.export import build_batch
    from mindie_knowledge.loop import settings as settings_mod
    store.create_draft(kind='experience', title='K3 source observation', summary='Reported, unverified.',
                       content='Pending source material retains its uncertainty.', generation=generation)
    path = store.root.parents[1] / 'community.json'
    return build_batch(store, settings=settings_mod.load(path))[0]


def _submit(tmp_path, consent):
    world = _world(tmp_path, enabled=True, consent=consent)
    called = []

    def submit_batch(*args, **kwargs):
        called.append((args, kwargs))
        return {"status": "submitted", "detail": "recorder", "head_sha": "a"*40, "pr_url":"https://github.com/example/repo/pull/1"}

    world["engine"].community = {
        "submit_batch": submit_batch,
        "reconcile_batch": lambda *args, **kwargs: {"status": "unknown"},
    }
    generation = json.loads(world["settings"].read_text())["generation"]
    batch_id = _pending_batch(world["store"], generation)
    world["batch_id"] = batch_id
    world["engine"]._submit(world["store"].batch(batch_id))
    return world, called


def test_contribute_consent_reaches_the_write_boundary(tmp_path):
    world, called = _submit(tmp_path, ("contribute", "later"))
    try:
        assert len(called) == 1
    finally:
        _cleanup(world)


@pytest.mark.parametrize(
    "consent",
    ["corrupt", ("read-only", "disabled"), ("later", "later")],
    ids=["corrupt", "read-only", "later"],
)
def test_non_contribute_consent_does_not_submit(tmp_path, consent):
    world, called = _submit(tmp_path, consent)
    try:
        assert called == []
        row = world["store"].batch(world["batch_id"])
        assert row["status"] == "pending"
        assert row["attempted"] is None
    finally:
        _cleanup(world)


@pytest.mark.parametrize("consent", _SHAPES)
def test_malformed_gate_does_not_submit(tmp_path, consent):
    world, called = _submit(tmp_path, consent)
    try:
        assert called == [], called
        row = world["store"].batch(world["batch_id"])
        assert row["status"] == "pending", row
        assert row["attempted"] is None
    finally:
        _cleanup(world)
