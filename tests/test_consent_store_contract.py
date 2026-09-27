"""Independent contract for mindie_knowledge.consent_store.

The parallel design fixes the names read / record_choice / record_reporting /
migrate and the mindie-consent/1 semantics. These tests judge the file on
disk and the returned state. They do not skip when the module is absent.
"""

import json
import os
import stat
import subprocess
import sys
import textwrap

import pytest

from lane_support import CHOICES, CONSENT_SCHEMA, REPORTING, REPO, reap

_CHILD = textwrap.dedent(
    """\
    import json, os, sys, time
    sys.path.insert(0, os.environ["GROK_CORE_REPO"])
    from mindie_knowledge import consent_store
    path = os.environ["CONSENT_PATH"]
    mode = sys.argv[1]
    loops = int(sys.argv[2])
    errors = 0
    if mode == "choice":
        for i in range(loops):
            consent_store.record_choice(path, "contribute" if i % 2 == 0 else "read-only")
    elif mode == "reporting":
        for i in range(loops):
            consent_store.record_reporting(path, "enabled" if i % 2 == 0 else "later")
    elif mode == "read":
        deadline = time.time() + float(sys.argv[3])
        while time.time() < deadline:
            try:
                raw = open(path, "rb").read()
            except FileNotFoundError:
                errors += 1
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                errors += 1
                continue
            if not isinstance(data, dict) or data.get("schema") != "mindie-consent/1":
                errors += 1
        print(errors)
    else:
        raise SystemExit("unknown mode")
    """
)


@pytest.fixture
def api():
    try:
        from mindie_knowledge import consent_store
    except ImportError as exc:
        pytest.fail(
            "mindie_knowledge.consent_store is not importable; "
            f"kimi-core has not published the shared store ({exc})"
        )
    for name in ("read", "record_choice", "record_reporting", "migrate"):
        if not callable(getattr(consent_store, name, None)):
            pytest.fail(f"consent_store.{name} is missing")
    return consent_store


def _bytes(path):
    return path.read_bytes() if path.exists() else None


def _load(path):
    return json.loads(path.read_text())


def _view(result):
    assert isinstance(result, dict), result
    assert result["state"] in {"ok", "missing", "unreadable", "corrupt"}
    return result


def test_read_distinguishes_missing_unreadable_and_corrupt_without_writing(api, tmp_path):
    missing = tmp_path / "missing.json"
    before = list(tmp_path.iterdir())
    found = api.read(missing)
    assert found["state"] == "missing"
    assert found["choice"] is None and found["reporting"] is None
    assert found["error"] is None
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == before

    unread = tmp_path / "unreadable.json"
    unread.write_text('{"schema":"mindie-consent/1","choice":"later"}\n')
    unread.chmod(0)
    try:
        with pytest.raises(PermissionError):
            unread.read_bytes()
        seen = api.read(unread)
        assert seen["state"] == "unreadable"
        assert seen["choice"] is None and seen["reporting"] is None
        assert isinstance(seen["error"], str) and seen["error"].strip()
    finally:
        unread.chmod(0o600)
    assert b'"choice":"later"' in unread.read_bytes() or b'"choice": "later"' in unread.read_bytes()

    samples = {
        "empty": b"",
        "text": b"{not json",
        "list": b"[1]\n",
        "schema": b'{"schema":"other/1","choice":"contribute"}\n',
        "choice": b'{"schema":"mindie-consent/1","choice":"public"}\n',
        "reporting": b'{"schema":"mindie-consent/1","choice":"later","reporting":"always"}\n',
        "huge": b'{"schema":"mindie-consent/1","choice":"later","pad":"' + (b"x" * 70000) + b'"}\n',
    }
    for name, raw in samples.items():
        path = tmp_path / f"{name}.json"
        path.write_bytes(raw)
        stamp = path.stat().st_mtime_ns
        seen = api.read(path)
        assert seen["state"] == "corrupt", name
        assert seen["choice"] is None and seen["reporting"] is None
        assert isinstance(seen["error"], str) and seen["error"].strip()
        assert path.read_bytes() == raw
        assert path.stat().st_mtime_ns == stamp

    healthy = tmp_path / "ok.json"
    healthy.write_text(
        json.dumps(
            {
                "schema": CONSENT_SCHEMA,
                "choice": "read-only",
                "reporting": "later",
            }
        )
        + "\n"
    )
    raw = healthy.read_bytes()
    stamp = healthy.stat().st_mtime_ns
    seen = api.read(healthy)
    assert seen["state"] == "ok"
    assert seen["choice"] == "read-only"
    assert seen["reporting"] == "later"
    assert seen["error"] in (None, "")
    assert healthy.read_bytes() == raw
    assert healthy.stat().st_mtime_ns == stamp


def test_record_merges_one_field_and_refuses_to_clear_a_bad_file(api, tmp_path):
    path = tmp_path / "consent.json"
    chosen = _view(api.record_choice(path, "later"))
    assert chosen["state"] == "ok" and chosen["choice"] == "later"
    reported = _view(api.record_reporting(path, "disabled"))
    assert reported["reporting"] == "disabled" and reported["choice"] == "later"
    seen = api.read(path)
    assert seen["state"] == "ok"
    assert seen["choice"] == "later"
    assert seen["reporting"] == "disabled"
    body = _load(path)
    assert body["schema"] == CONSENT_SCHEMA
    assert body["choice"] == "later" and body["reporting"] == "disabled"
    assert body.get("enabled") is not True
    assert "project_roots" not in body

    api.record_choice(path, "contribute")
    body = _load(path)
    assert body["choice"] == "contribute"
    assert body["reporting"] == "disabled"
    api.record_reporting(path, "later")
    body = _load(path)
    assert body["choice"] == "contribute"
    assert body["reporting"] == "later"

    untouched = path.read_bytes()
    with pytest.raises(ValueError):
        api.record_choice(path, "public")
    with pytest.raises(ValueError):
        api.record_reporting(path, "always")
    assert path.read_bytes() == untouched

    noted = tmp_path / "noted.json"
    noted.write_text(
        json.dumps(
            {
                "schema": CONSENT_SCHEMA,
                "choice": "later",
                "reporting": "later",
                "note": "keep",
                "migrated_from": ["legacy"],
            }
        )
        + "\n"
    )
    _view(api.record_reporting(noted, "disabled"))
    kept = _load(noted)
    assert kept["note"] == "keep"
    assert kept["choice"] == "later"
    assert kept["reporting"] == "disabled"
    assert kept.get("migrated_from") == ["legacy"]
    _view(api.record_choice(noted, "read-only"))
    after_choice = _load(noted)
    assert after_choice["note"] == "keep"
    assert after_choice["reporting"] == "disabled"
    assert "migrated_from" not in after_choice

    damaged = tmp_path / "damaged.json"
    damaged.write_bytes(b"{not json\n")
    broken = damaged.read_bytes()
    with pytest.raises(api.ConsentError):
        api.record_choice(damaged, "contribute")
    assert damaged.read_bytes() == broken

    # Unknown choice is corrupt. A field update must not replace it.
    raw = (
        b'{"schema":"mindie-consent/1","choice":"public","reporting":"later","note":"keep"}\n'
    )
    damaged.write_bytes(raw)
    with pytest.raises(api.ConsentError):
        api.record_reporting(damaged, "enabled")
    assert damaged.read_bytes() == raw


def test_migrate_writes_once_only_when_evidence_agrees(api, tmp_path):
    path = tmp_path / "consent.json"
    agreed = [
        {"choice": "read-only", "reporting": "disabled", "source": "kimi-marker"},
        {"choice": "read-only", "reporting": "disabled", "source": "cc-marker"},
    ]
    result = _view(api.migrate(path, agreed))
    assert result["status"] == "migrated"
    seen = api.read(path)
    assert seen["state"] == "ok"
    assert seen["choice"] == "read-only"
    assert seen["reporting"] == "disabled"
    written = path.read_bytes()
    again = _view(api.migrate(path, agreed))
    assert again["status"] == "kept"
    assert path.read_bytes() == written
    assert api.read(path)["choice"] == "read-only"

    conflict = tmp_path / "conflict.json"
    failed = _view(
        api.migrate(
            conflict,
            [
                {"choice": "later", "reporting": "later", "source": "kimi"},
                {"choice": "contribute", "reporting": "later", "source": "codex"},
            ],
        )
    )
    assert failed["status"] == "conflict"
    assert failed["error"] or failed["detail"]
    assert not conflict.exists()

    reporting_conflict = tmp_path / "reporting.json"
    failed = _view(
        api.migrate(
            reporting_conflict,
            [
                {"choice": "later", "reporting": "disabled", "source": "kimi"},
                {"choice": "later", "reporting": "enabled", "source": "cc"},
            ],
        )
    )
    assert failed["status"] == "conflict"
    assert not reporting_conflict.exists()

    # A source with no reporting evidence does not contradict the one that has it.
    filled = tmp_path / "filled.json"
    filled_result = _view(
        api.migrate(
            filled,
            [
                {"choice": "later", "reporting": "disabled", "source": "kimi"},
                {"choice": "later", "reporting": None, "source": "cc"},
            ],
        )
    )
    assert filled_result["status"] == "migrated"
    assert api.read(filled)["choice"] == "later"
    assert api.read(filled)["reporting"] == "disabled"

    bad = tmp_path / "bad.json"
    with pytest.raises(ValueError):
        api.migrate(
            bad,
            [{"choice": "yes-please", "reporting": "enabled", "source": "mystery"}],
        )
    assert not bad.exists()
    empty = tmp_path / "empty.json"
    absent = _view(api.migrate(empty, []))
    assert absent["status"] == "absent"
    assert not empty.exists()


def test_migrate_keeps_existing_authority_and_corrupt_bytes(api, tmp_path):
    path = tmp_path / "consent.json"
    path.write_text(
        json.dumps(
            {
                "schema": CONSENT_SCHEMA,
                "choice": "later",
                "reporting": "later",
                "source": "previous",
            }
        )
        + "\n"
    )
    original = path.read_bytes()
    result = _view(
        api.migrate(
            path,
            [{"choice": "contribute", "reporting": "enabled", "source": "upgrade"}],
        )
    )
    assert result["status"] == "kept"
    assert path.read_bytes() == original
    assert api.read(path)["choice"] == "later"
    assert api.read(path)["reporting"] == "later"

    corrupt = tmp_path / "corrupt.json"
    blob = b"{not-valid-consent"
    corrupt.write_bytes(blob)
    failed = _view(
        api.migrate(
            corrupt,
            [{"choice": "contribute", "reporting": "enabled", "source": "guess"}],
        )
    )
    assert failed["status"] == "error"
    assert failed["error"] or failed["detail"]
    assert corrupt.read_bytes() == blob


def test_cross_process_updates_merge_and_never_tear(api, tmp_path):
    path = tmp_path / "consent.json"
    api.record_choice(path, "later")
    api.record_reporting(path, "disabled")
    script = tmp_path / "worker.py"
    script.write_text(_CHILD)
    env = os.environ.copy()
    env["GROK_CORE_REPO"] = str(REPO)
    env["CONSENT_PATH"] = str(path)
    loops = "25"
    commands = []
    for mode in ("choice", "reporting"):
        for _ in range(3):
            commands.append(
                [
                    sys.executable,
                    str(script),
                    mode,
                    loops,
                ]
            )
    reader = None
    workers = []
    try:
        reader = subprocess.Popen(
            [sys.executable, str(script), "read", "1", "2.5"],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        workers = [
            subprocess.Popen(
                command,
                cwd=tmp_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for command in commands
        ]
        failures = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=30)
            if worker.returncode != 0:
                failures.append((worker.returncode, stdout, stderr))
        reader_out, reader_err = reader.communicate(timeout=30)
        assert not failures, failures
        assert reader.returncode == 0, reader_err
        assert int(reader_out.strip() or "0") == 0, reader_err
    finally:
        for worker in workers:
            reap(worker)
        reap(reader)
    body = _load(path)
    assert body["schema"] == CONSENT_SCHEMA
    assert body["choice"] in CHOICES
    assert body["reporting"] in REPORTING
    seen = api.read(path)
    assert seen["state"] == "ok"
    assert seen["choice"] == body["choice"]
    assert seen["reporting"] == body["reporting"]
    names = {item.name for item in path.parent.iterdir()}
    unexpected = sorted(
        name for name in names if name not in {path.name, script.name} and not name.endswith(".lock")
    )
    assert unexpected == []
    assert stat.S_ISREG(path.stat().st_mode)
