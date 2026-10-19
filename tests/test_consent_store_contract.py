"""Independent contract for mindie_knowledge.consent_store.

The current contract exposes read / record_choice and
the mindie-consent/1 semantics. These tests judge the file on
disk and the returned state. They do not skip when the module is absent.
"""

import json
import os
import stat
import subprocess
import sys
import textwrap

import pytest

from lane_support import CHOICES, CONSENT_SCHEMA, REPO, allow_read, deny_read, reap

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
    elif mode == "read":
        # Product reads release each handle before parsing the snapshot.
        deadline = time.time() + float(sys.argv[3])
        choices = {"contribute", "read-only", "later", "disabled"}
        while time.time() < deadline:
            seen = consent_store.read(path)
            if (
                seen.get("state") != "ok"
                or seen.get("choice") not in choices
                or not seen.get("choice_revision")
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
    for name in ("read", "record_choice"):
        if not callable(getattr(consent_store, name, None)):
            pytest.fail(f"consent_store.{name} is missing")
    return consent_store


def _bytes(path):
    return path.read_bytes() if path.exists() else None


def test_repeating_contribution_choice_is_a_byte_identical_noop(api, tmp_path):
    path = tmp_path / 'consent.json'
    api.record_choice(path, 'contribute')
    before = path.read_bytes(), path.stat().st_mtime_ns
    api.record_choice(path, 'contribute')
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    api.record_choice(path, 'disabled')
    assert api.read(path)['choice'] == 'disabled'
    api.record_choice(path, 'contribute')
    assert api.read(path)['choice'] == 'contribute'
    assert path.read_bytes() != before[0]


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


def test_record_preserves_unrelated_metadata_and_refuses_damaged_authority(api, tmp_path):
    path = tmp_path / "consent.json"
    path.write_text(json.dumps({"schema": CONSENT_SCHEMA, "choice": "later",
                                "reporting": "disabled", "note": "keep"}))
    result = api.record_choice(path, "contribute")
    assert result["choice"] == "contribute" and result["choice_revision"]
    assert _load(path)["note"] == "keep"
    assert _load(path)["reporting"] == "disabled"
    for raw in (b"{broken", b'{"schema":"mindie-consent/1","choice":"public"}'):
        path.write_bytes(raw)
        with pytest.raises(api.ConsentError):
            api.record_choice(path, "contribute")
        assert path.read_bytes() == raw


def test_oversized_authority_is_bounded_and_never_rewritten(api, tmp_path):
    path = tmp_path / "oversized.json"
    # Valid JSON plus excess whitespace must not pass just because parsing
    # the bounded prefix would succeed.
    raw = b'{"schema":"mindie-consent/1","choice":"contribute"}' + b" " * api.MAX_BYTES
    path.write_bytes(raw)
    assert len(api._read_bytes(path)) == api.MAX_BYTES + 1
    assert api.read(path)["state"] == "corrupt"
    with pytest.raises(api.ConsentError) as failure:
        api.record_choice(path, "disabled")
    assert failure.value.state == "corrupt"
    assert path.read_bytes() == raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO boundary")
def test_fifo_without_writer_fails_without_blocking_or_replacement(api, tmp_path):
    path = tmp_path / "consent.fifo"
    os.mkfifo(path)
    identity = path.stat().st_ino
    code = """
import json, sys
from mindie_knowledge import consent_store
path = sys.argv[1]
result = consent_store.read(path)
try:
    consent_store.record_choice(path, "contribute")
except consent_store.ConsentError as exc:
    result["write_state"] = exc.state
else:
    raise AssertionError("FIFO must not be overwritten")
print(json.dumps(result))
"""
    result = subprocess.run([sys.executable, "-c", code, str(path)],
                            capture_output=True, text=True, timeout=3, check=True)
    view = json.loads(result.stdout)
    assert view["state"] == view["write_state"] == "unreadable"
    assert stat.S_ISFIFO(path.stat().st_mode)
    assert path.stat().st_ino == identity


def test_cross_process_updates_merge_and_never_tear(api, tmp_path):
    path = tmp_path / "consent.json"
    api.record_choice(path, "later")
    api.record_choice(path, "disabled")
    script = tmp_path / "worker.py"
    script.write_text(_CHILD)
    env = os.environ.copy()
    env["GROK_CORE_REPO"] = str(REPO)
    env["CONSENT_PATH"] = str(path)
    loops = "25"
    commands = []
    for mode in ("choice",):
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
    assert body["choice_revision"]
    seen = api.read(path)
    assert seen["state"] == "ok"
    assert seen["choice"] == body["choice"]
    assert seen["choice_revision"] == body["choice_revision"]
    names = {item.name for item in path.parent.iterdir()}
    unexpected = sorted(
        name for name in names if name not in {path.name, script.name} and not name.endswith(".lock")
    )
    assert unexpected == []
    assert stat.S_ISREG(path.stat().st_mode)


@pytest.mark.parametrize("filename", ["consent.json", "同意-\U0001F600.json"])
def test_product_read_closes_before_atomic_publication(api, tmp_path, filename):
    """Product reads return a complete snapshot without retaining a handle."""
    path = tmp_path / filename
    api.record_choice(path, "later")
    api.record_choice(path, "disabled")
    old = api._read_bytes(path)
    result = api.record_choice(path, "contribute")
    assert result["state"] == "ok", result
    assert result["choice"] == "contribute"
    assert json.loads(old)["choice"] == "disabled"
    disk = json.loads(path.read_bytes())
    assert disk["schema"] == CONSENT_SCHEMA
    assert disk["choice"] == "contribute"
    assert disk["choice_revision"] == result["choice_revision"]

