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

from lane_support import CHOICES, CONSENT_SCHEMA, REPORTING, REPO, allow_read, deny_read, reap

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
        # Product API only. A stdlib open() denies FILE_SHARE_DELETE on
        # Windows and would block os.replace; that is a different observer.
        deadline = time.time() + float(sys.argv[3])
        choices = {"contribute", "read-only", "later", "disabled"}
        reporting = {"enabled", "disabled", "later"}
        while time.time() < deadline:
            seen = consent_store.read(path)
            if (
                seen.get("state") != "ok"
                or seen.get("choice") not in choices
                or seen.get("reporting") not in reporting
            ):
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
    unread.write_text('{"schema":"mindie-consent/1","choice":"later"}\n', encoding="utf-8", newline="\n")
    deny_read(unread)
    try:
        with pytest.raises(PermissionError):
            unread.read_bytes()
        seen = api.read(unread)
        assert seen["state"] == "unreadable"
        assert seen["choice"] is None and seen["reporting"] is None
        assert isinstance(seen["error"], str) and seen["error"].strip()
    finally:
        allow_read(unread)
    assert b'"choice":"later"' in unread.read_bytes() or b'"choice": "later"' in unread.read_bytes()

    samples = {
        "empty": b"",
        "text": b"{not json",
        "list": b"[1]\n",
        "schema": b'{"schema":"other/1","choice":"contribute"}\n',
        "choice": b'{"schema":"mindie-consent/1","choice":"public"}\n',
        "reporting": b'{"schema":"mindie-consent/1","choice":"later","reporting":"always"}\n',
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


def _product_read_stream(api, path):
    """The handle ``read`` actually holds, not a third-party open().

    Current ``d0538`` reads with ``path.read_bytes()`` (stdlib ``open``).
    A later product may publish ``_open_for_read``. This does not reimplement
    either opener.
    """
    opener = getattr(api, "_open_for_read", None)
    if opener is None:
        return open(path, "rb")
    return opener(path)


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
            encoding="utf-8",
            errors="replace",
        )
        workers = [
            subprocess.Popen(
                command,
                cwd=tmp_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for command in commands
        ]
        failures = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=30)
            if worker.returncode != 0:
                failures.append((worker.returncode, stdout, stderr))
        reader_out, reader_err = reader.communicate(timeout=30)
        if failures:
            rendered = "\n====\n".join(
                f"exit {code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
                for code, out, err in failures
            )
            pytest.fail("consent workers failed\n" + rendered)
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


@pytest.mark.parametrize("filename", ["consent.json", "同意-\U0001F600.json"])
def test_held_product_read_allows_atomic_publication(api, tmp_path, filename):
    """One held product read must not stop a serialized publication.

    Sharing delete on the reader does not make ``os.replace`` succeed on
    Windows. The product writer must still publish both fields while this
    handle is open. The non-BMP filename is a real path, not an escaped
    ASCII substitute.
    """
    path = tmp_path / filename
    api.record_choice(path, "later")
    api.record_reporting(path, "disabled")
    held = _product_read_stream(api, path)
    try:
        result = api.record_choice(path, "contribute")
    finally:
        held.close()
    assert result["state"] == "ok", result
    assert result["choice"] == "contribute"
    assert result["reporting"] == "disabled"
    disk = json.loads(path.read_bytes())
    assert disk["schema"] == CONSENT_SCHEMA
    assert disk["choice"] == "contribute"
    assert disk["reporting"] == "disabled"


@pytest.mark.skipif(
    os.name != "nt",
    reason=(
        "POSIX os.replace is not blocked by an open reader. On Windows a "
        "stdlib open denies FILE_SHARE_DELETE; that external handle is not "
        "a product read and is not what the stress test observes."
    ),
)
def test_nonsharing_external_handle_preserves_bytes(api, tmp_path):
    """A third-party handle that denies delete sharing fails one publication.

    The bytes already on disk stay. The test does not retry the writer.
    """
    path = tmp_path / "consent.json"
    api.record_choice(path, "later")
    api.record_reporting(path, "disabled")
    before = path.read_bytes()
    held = open(path, "rb")
    try:
        with pytest.raises(PermissionError):
            api.record_choice(path, "contribute")
    finally:
        held.close()
    assert path.read_bytes() == before
    seen = api.read(path)
    assert seen["state"] == "ok"
    assert seen["choice"] == "later"
    assert seen["reporting"] == "disabled"
