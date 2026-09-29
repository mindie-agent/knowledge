"""Support for grok-core independent tests.

Loads the frozen adapter parsers from the source candidate trees. Those
trees are read only. This module does not implement product behavior and
does not substitute ``transcript_double``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PARSER_NAMES = ("kimi", "cc", "codex")
_PARSER_RELATIVE = {
    "kimi": Path("kimi/scripts/transcript.py"),
    "cc": Path("cc/scripts/transcript.py"),
    "codex": Path("codex/plugins/mindie-agent/scripts/codex_transcript.py"),
}


class MissingParserCheckout(RuntimeError):
    """A required frozen parser file was not given. Not a skip."""


def isolation_root():
    """Directory conftest created for HOME and diagnostics. Not a lane name."""
    raw = os.environ.get("MINDIE_KNOWLEDGE_TEST_ROOT")
    if not raw:
        raise RuntimeError(
            "MINDIE_KNOWLEDGE_TEST_ROOT is not set. The test conftest creates "
            "it, or the runner may set it to an explicit temporary directory."
        )
    return Path(raw)


def parser_path(name):
    """Resolve one frozen parser to an absolute file.

    ``MINDIE_PARSER_*`` overrides ``MINDIE_FRAMEWORK_SOURCE``. A relative
    value is resolved from the current directory, then returned absolute so
    a later working-directory change still opens that file. Never a
    production install and never a synthesized double.
    """
    if name not in _PARSER_RELATIVE:
        raise MissingParserCheckout(
            f"unknown parser {name!r}; expected one of {', '.join(PARSER_NAMES)}"
        )
    override = os.environ.get(f"MINDIE_PARSER_{name.upper()}")
    if override:
        path = Path(override).expanduser()
        origin = f"MINDIE_PARSER_{name.upper()}"
    else:
        root = os.environ.get("MINDIE_FRAMEWORK_SOURCE")
        if not root:
            relative = _PARSER_RELATIVE[name]
            raise MissingParserCheckout(
                f"parser {name!r} is not configured. Set MINDIE_PARSER_{name.upper()} "
                f"to that transcript module, or MINDIE_FRAMEWORK_SOURCE to a checkout "
                f"containing {relative}. A production install is not a substitute."
            )
        path = Path(root).expanduser() / _PARSER_RELATIVE[name]
        origin = "MINDIE_FRAMEWORK_SOURCE"
    path = path.resolve()
    if not path.is_file():
        raise MissingParserCheckout(
            f"parser {name!r} from {origin} is not a file: {path}"
        )
    return path


def parser_paths():
    return {name: parser_path(name) for name in PARSER_NAMES}


SESSIONS = {
    "kimi": "ses_core",
    "cc": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "codex": "task-core",
}
CONSENT_SCHEMA = "mindie-consent/1"
CHOICES = ("contribute", "read-only", "later", "disabled")
REPORTING = ("enabled", "disabled", "later")


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_parser(name):
    """Load one production parser module from its frozen source path."""
    path = parser_path(name)
    spec = importlib.util.spec_from_file_location(
        f"grok_core_frozen_{name}_transcript", path
    )
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve the class module through sys.modules during exec.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for attr in ("FileIdentity", "identify", "read_material"):
        if not hasattr(module, attr):
            raise RuntimeError(f"{path} does not export {attr}")
    if "scan_until" not in module.read_material.__code__.co_varnames:
        raise RuntimeError(f"{path} read_material has no scan_until parameter")
    return module


def iso(when):
    return datetime.fromtimestamp(when, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def transcript_path(name, root, session):
    if name == "kimi":
        path = (
            Path(root)
            / "sessions"
            / "wd_lane"
            / session
            / "agents"
            / "main"
            / "wire.jsonl"
        )
    elif name == "cc":
        path = Path(root) / f"{session}.jsonl"
    elif name == "codex":
        path = Path(root) / "rollout.jsonl"
    else:
        raise ValueError(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if name == 'kimi':
        # Real Kimi sessions have a separate lineage authority. Do not let
        # a stateless synthetic fixture silently stand in for a root task.
        state = path.parents[2] / 'state.json'
        if not state.exists():
            state.write_text(json.dumps(dict(createdAt=int(time.time() * 1000))), encoding='utf-8')
    return path


def encode_record(name, session, text, when):
    if name == "kimi":
        record = {
            "type": "context.append_message",
            "time": int(when * 1000),
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
                "origin": {"kind": "user"},
            },
        }
    elif name == "cc":
        record = {
            "type": "user",
            "sessionId": session,
            "timestamp": iso(when),
            "message": {"role": "user", "content": text},
        }
    elif name == "codex":
        record = {
            "timestamp": iso(when),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
    else:
        raise ValueError(name)
    return json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"


def encode_header(name, session, when, *, fork_parent=None):
    if name != "codex":
        return b""
    payload = {"id": session, "cwd": "/work", "timestamp": iso(when)}
    if fork_parent:
        payload["forked_from_id"] = fork_parent
    return json.dumps({"type": "session_meta", "payload": payload}).encode() + b"\n"


def write_transcript(name, path, session, texts, when, *, fork_parent=None):
    blob = encode_header(name, session, when, fork_parent=fork_parent)
    for offset, text in enumerate(texts):
        blob += encode_record(name, session, text, when + offset)
    path.write_bytes(blob)
    return blob


def append_record(name, path, session, text, when):
    blob = encode_record(name, session, text, when)
    with open(path, "ab") as handle:
        handle.write(blob)
    return blob


def entry_documents(store):
    with store.lock:
        rows = store.db.execute("SELECT doc FROM entries").fetchall()
    return [row[0] for row in rows]


def unfinished_captures(store):
    with store.lock:
        return [
            dict(row)
            for row in store.db.execute(
                "SELECT id, status, detail FROM captures WHERE status IN "
                "('queued','pending','deferred','processing','apply-pending') "
                "ORDER BY created"
            )
        ]


def regions(store):
    with store.lock:
        return [
            dict(row)
            for row in store.db.execute("SELECT * FROM regions ORDER BY created")
        ]


def spawn_count(path):
    if not path.exists():
        return 0
    return path.read_text().count("x")


def installed_python():
    return sys.executable


def kill_owned_group(pid):
    """Stop one test-owned process tree. Never a system-wide scan.

    POSIX signals the process group (the leader pid remains the group id
    after that leader exits). Windows has no ``killpg``; ``taskkill /T``
    stops that pid and the children it still owns.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return
    os.killpg(pid, signal.SIGKILL)


def _icacls(path, *args):
    """One icacls change. A non-zero exit is a test failure, not a skip."""
    completed = subprocess.run(
        ["icacls", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise AssertionError(
            "icacls "
            + " ".join(args)
            + f" exited {completed.returncode}\n"
            + completed.stdout
            + completed.stderr
        )
    return completed


def deny_read(path):
    """Make a subsequent read raise ``PermissionError``.

    Mode ``0`` does that on POSIX. On Windows ``(R)`` also denies
    ``READ_CONTROL``, so later ``icacls`` cannot repair the DACL. Deny only
    ``RD`` (read data) and leave the DACL readable. The caller must
    ``allow_read`` in ``finally``.
    """
    path = Path(path)
    if os.name == "nt":
        _icacls(path, "/deny", f"{_windows_user_sid()}:(RD)")
        return
    path.chmod(0)


def allow_read(path):
    """Reverse ``deny_read`` so the test can inspect the preserved bytes."""
    path = Path(path)
    if os.name == "nt":
        _icacls(path, "/remove:d", _windows_user_sid())
        return
    path.chmod(0o600)


def _windows_user_sid():
    """Target the current user, even when the computer has the same name.

    An unqualified USERNAME can resolve to the computer/domain SID rather
    than its user. icacls reports success but that ACE does not deny reads.
    Numeric SIDs use the documented leading '*' icacls syntax.
    """
    import csv
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, timeout=10,
    )
    rows = list(csv.reader(result.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) != 2 or not rows[0][1].startswith("S-1-"):
        raise RuntimeError("cannot resolve the current Windows user SID")
    return "*" + rows[0][1]


def reap(process, *, group=False):
    """Stop one test-owned process. Group mode is only for start_new_session.

    On POSIX the leader pid is the process-group id, including after that
    leader has exited. On Windows the same call stops the owned tree with
    ``taskkill /T``. This never scans the system for unrelated processes.
    """
    if process is None:
        return
    if os.name == "nt":
        if group or process.poll() is None:
            try:
                kill_owned_group(process.pid)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
    elif group:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
    elif process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
