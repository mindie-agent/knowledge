"""Anonymous K3 restart/authority dimensions through actual parser and scanner.
A fault is not revocation; saved model output resumes without another call.
"""
import json
import pytest
from conftest import make_admission, write_settings
from lane_support import load_parser, transcript_path, write_transcript, entry_documents
from material_worker_fixture import command as summary_command
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_redaction import install_scanner
from mindie_knowledge.loop.transcript_capture import summarize_due
from mindie_knowledge.loop import transcript_capture
from mindie_knowledge import consent_store

TOKEN = 'resumed precision observation remains unresolved'


def _world(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    consent, settings = tmp_path / 'consent.json', tmp_path / 'settings.json'
    consent_store.record_choice(consent, 'contribute')
    consent_store.record_reporting(consent, 'later')
    parsed = write_settings(settings, enabled=True, roots=[project], consent_config=str(consent))
    admission = Admission(make_admission(tmp_path, project_root=project, session='ses-fault'))
    source = transcript_path('codex', tmp_path / 'logs', 'ses-fault')
    store = Store(tmp_path / 'store', 'test')
    marker = tmp_path / 'calls'
    engine = Engine(store, settings_path=settings, admission=admission,
                    transcript_adapter=load_parser('codex'), redactor_executable=install_scanner(),
                    summary_command=summary_command(calls=marker))
    when = max(parsed.enabled_at, admission.active_lease('ses-fault')['activated_at']) + 1
    write_transcript('codex', source, 'ses-fault', [TOKEN], when)
    return dict(store=store, engine=engine, settings=settings, consent=consent,
                marker=marker, project=project, source=source, saved=consent.read_bytes(),
                settings_bytes=settings.read_bytes(), generation=parsed.generation)


def _capture(world):
    return world['engine'].capture(session_id='ses-fault', turn_id='one', transcript_path=str(world['source']))


def _spawns(world):
    return len(world['marker'].read_text()) if world['marker'].exists() else 0


def _tick(world):
    world['engine'].last_activity = 0
    with world['store']._write_txn():
        world['store'].db.execute('UPDATE transcript_tasks SET summary_due=0')
    summarize_due(world['engine'])

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



@pytest.mark.parametrize('kind', ['corrupt', *_MALFORMED])
def test_k3_authority_fault_keeps_admitted_material_and_same_choice_resumes(tmp_path, kind):
    world = _world(tmp_path)
    store, engine = world['store'], world['engine']
    try:
        captured = _capture(world)
        (_corrupt(world) if kind == 'corrupt' else _introduce(world, kind))
        engine._process(captured['id'])
        assert store.capture_row(captured['id'])['status'] in _KEEP
        assert store.capture_row(captured['id'])['transcript'] == str(world['source'])
        assert _spawns(world) == 0 and entry_documents(store) == []
        (_restore(world) if kind == 'corrupt' else _repair(world, kind))
        engine._process(captured['id'])
        _tick(world)
        assert store.capture_row(captured['id'])['status'] == 'organized'
        assert TOKEN in '\n'.join(entry_documents(store)) and _spawns(world) == 1
        assert engine._settings().generation == world['generation']
    finally:
        store.close()


@pytest.mark.parametrize('kind', ['corrupt', 'null-config', 'enabled-string-false', 'choice-absent'])
def test_k3_saved_model_result_survives_authority_fault_without_another_call(tmp_path, monkeypatch, kind):
    world = _world(tmp_path)
    store, engine = world['store'], world['engine']
    try:
        captured = _capture(world)
        engine._process(captured['id'])
        assert store.capture_row(captured['id'])['status'] == 'organized', store.capture_row(captured['id'])
        assert TOKEN in '\n'.join(entry_documents(store))
        real = transcript_capture.bounded_run
        def corrupt_after_return(command, *args, **kwargs):
            response = real(command, *args, **kwargs)
            if '--identity' not in command:
                (_corrupt(world) if kind == 'corrupt' else _introduce(world, kind))
            return response
        monkeypatch.setattr(transcript_capture, 'bounded_run', corrupt_after_return)
        _tick(world)
        attempt = dict(store.db.execute('SELECT * FROM material_summary_attempts').fetchone())
        assert attempt['status'] == 'returned' and attempt['response']
        assert _spawns(world) == 1
        _tick(world)
        assert _spawns(world) == 1
        (_restore(world) if kind == 'corrupt' else _repair(world, kind))
        monkeypatch.setattr(transcript_capture, 'bounded_run', lambda *a, **k: pytest.fail('saved result called worker again'))
        _tick(world)
        assert store.db.execute('SELECT status FROM material_summary_attempts').fetchone()[0] == 'complete'
        assert engine.status()['summary_usage']['model_calls'] == 1
        assert engine._settings().generation == world['generation']
    finally:
        store.close()


@pytest.mark.parametrize('choice', ['read-only', 'later', 'disabled', 'boolean-false'])
def test_explicit_revocation_cancels_capture_and_does_not_resurrect(tmp_path, choice):
    world = _world(tmp_path)
    store, engine = world['store'], world['engine']
    try:
        captured = _capture(world)
        if choice == 'boolean-false':
            _patch_settings(world, enabled=False)
        else:
            consent_store.record_choice(world['consent'], choice)
        engine._process(captured['id'])
        assert store.capture_row(captured['id'])['status'] == 'cancelled'
        assert _spawns(world) == 0
        world['settings'].write_bytes(world['settings_bytes'])
        _restore(world)
        engine._process(captured['id'])
        assert store.capture_row(captured['id'])['status'] == 'cancelled'
        assert entry_documents(store) == []
    finally:
        store.close()
