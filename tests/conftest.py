"""Shared pytest fixtures for the knowledge loop test-suite.

Isolation is applied at import, before product modules resolve ``Path.home``
or the diagnostics policy. The directory is ``MINDIE_KNOWLEDGE_TEST_ROOT``
when the runner sets it, otherwise a fresh temporary directory. Reporting
stays explicitly disabled.
"""

import json
import os
import tempfile
from pathlib import Path

from lane_support import PARSER_NAMES, parser_path

_REPO = Path(__file__).resolve().parents[1]
_COMMITTED_PARSERS = _REPO / "tests" / "fixtures" / "production-parsers"
_PARSER_FILES = (
    "kimi/scripts/transcript.py",
    "cc/scripts/transcript.py",
    "codex/plugins/mindie-agent/scripts/codex_transcript.py",
)


def _framework_source_errors():
    """Use the committed parsers when unset. An explicit path is never replaced."""
    raw = os.environ.get("MINDIE_FRAMEWORK_SOURCE", "").strip()
    if not raw:
        os.environ["MINDIE_FRAMEWORK_SOURCE"] = str(_COMMITTED_PARSERS)
        root = _COMMITTED_PARSERS
        origin = "committed tests/fixtures/production-parsers"
    else:
        root = Path(raw).expanduser()
        origin = "MINDIE_FRAMEWORK_SOURCE=" + raw
    if not root.is_dir():
        return [f"setup: {origin} is not a directory: {root}"]
    missing = [rel for rel in _PARSER_FILES if not (root / rel).is_file()]
    if missing:
        return [f"setup: {origin} missing {', '.join(missing)}"]
    return []


_FIXTURE_ERRORS = _framework_source_errors()

_explicit_root = os.environ.get("MINDIE_KNOWLEDGE_TEST_ROOT")
if _explicit_root:
    ISOLATION_ROOT = Path(_explicit_root).expanduser().resolve()
else:
    ISOLATION_ROOT = Path(tempfile.mkdtemp(prefix="mindie-knowledge-test-"))
os.environ["MINDIE_KNOWLEDGE_TEST_ROOT"] = str(ISOLATION_ROOT)
REAL_HOME = Path(os.environ.get("HOME") or str(Path.home()))
_WATCH = [
    REAL_HOME / ".config" / "mindie-agent" / "diagnostics.json",
    REAL_HOME / ".local" / "state" / "mindie" / "diagnostics",
]
if not _FIXTURE_ERRORS:
    for _parser_name in PARSER_NAMES:
        if os.environ.get("MINDIE_FRAMEWORK_SOURCE") or os.environ.get(
            f"MINDIE_PARSER_{_parser_name.upper()}"
        ):
            _WATCH.append(parser_path(_parser_name))


def _snapshot(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    return {
        "exists": True,
        "mtime_ns": info.st_mtime_ns,
        "size": info.st_size,
        "is_symlink": path.is_symlink(),
    }


ISOLATION_BEFORE = {str(path): _snapshot(path) for path in _WATCH}
ISOLATION_ROOT.mkdir(parents=True, exist_ok=True)
(ISOLATION_ROOT / "watched-before.json").write_text(
    json.dumps(ISOLATION_BEFORE, indent=2) + "\n"
)

_home = ISOLATION_ROOT / "home"
_config = ISOLATION_ROOT / "config"
_state = ISOLATION_ROOT / "state"
_diag_root = ISOLATION_ROOT / "diagnostics"
for _directory in (_home, _config, _state, _diag_root):
    _directory.mkdir(parents=True, exist_ok=True)
_real_gitleaks = REAL_HOME / ".cache" / "mindie-agent" / "gitleaks"
_isolated_gitleaks = _home / ".cache" / "mindie-agent" / "gitleaks"
if _real_gitleaks.is_dir() and not _isolated_gitleaks.exists():
    import shutil as _shutil
    _isolated_gitleaks.parent.mkdir(parents=True, exist_ok=True)
    _shutil.copytree(_real_gitleaks, _isolated_gitleaks)
os.environ.setdefault("MINDIE_DISABLE_FSYNC", "1")
os.environ["HOME"] = str(_home)
os.environ["USERPROFILE"] = str(_home)
os.environ["XDG_CONFIG_HOME"] = str(_config)
os.environ["XDG_STATE_HOME"] = str(_state)
os.environ["MINDIE_DIAGNOSTICS_ROOT"] = str(_diag_root)
_policy_path = ISOLATION_ROOT / "diagnostics.json"
os.environ["MINDIE_DIAGNOSTICS_CONFIG"] = str(_policy_path)
_policy_path.write_text(
    json.dumps(
        {
            "schema": "mindie.diagnostics.reporting.v1",
            "purpose": "tool_fault_reporting",
            "decision": "disabled",
            "repository": "mindie-agent/knowledge",
            "revision": "b34851a5df99f05787fd3c63ed6f6819",
            "roots": [str(ISOLATION_ROOT)],
        },
        indent=2,
    )
    + "\n"
)
os.chmod(_policy_path, 0o600)

import pytest

@pytest.fixture(autouse=True)
def lifecycle_tests_use_explicit_process_backend(request, monkeypatch):
    """These suites test real locks/RPC/handoff, not the platform service manager.

    CI has no interactive login token. The scheduler's own contract is tested
    separately; native lifetime acceptance runs on the real Windows host.
    This replacement is selected before a test, never after manager failure.
    """
    if request.node.path.name not in {
        "test_service_startup.py", "test_service_retirement.py",
    }:
        return
    import sys
    from mindie_knowledge.loop import process, service_host
    backend = service_host.platform_backend()
    if backend is None:
        return

    def direct_test_service(config_path, config):
        return process.spawn_service(
            [sys.executable, "-m", "mindie_knowledge.loop.cli", "serve",
             "--config", str(Path(config_path).resolve())],
            from_detached_starter=True,
        )

    monkeypatch.setattr(backend, "request_start", direct_test_service)


def pytest_configure(config):
    config.addinivalue_line("markers", "benchmark: large payload throughput benchmark")
    if _FIXTURE_ERRORS:
        pytest.exit("\n".join(_FIXTURE_ERRORS), returncode=2)


def pytest_collection_modifyitems(items):
    # One collection, each case once. Product output and process lifetime
    # failures surface before the cheaper but numerous leaf checks.
    priority = ('test_public_transcript.py', 'test_windows_process_ownership.py',
                'test_service_startup.py', 'test_production_parser_recovery.py')
    order = {name: index for index, name in enumerate(priority)}
    items.sort(key=lambda item: order.get(item.path.name, len(order)))


from mindie_knowledge.loop import settings as settings_mod
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path, "vllm-ascend")
    yield instance
    instance.close()


def write_settings(path, *, enabled=True, repository="mindie-agent/knowledge-vllm-ascend",
                   roots=None, idle_seconds=300, consent_choice=None, **extensions):
    """Write synthetic settings; only an explicit choice creates consent."""
    if consent_choice is not None:
        from mindie_knowledge import consent_store
        if "consent_config" in extensions:
            raise ValueError("fixture must choose a saved reference or an explicit choice")
        consent = Path(path).with_name(Path(path).name + ".consent.json")
        consent_store.record_choice(consent, consent_choice)
        extensions["consent_config"] = str(consent.resolve())
    return settings_mod.write(
        path,
        enabled=enabled,
        repository=repository,
        project_roots=roots or [],
        idle_seconds=idle_seconds,
        **extensions,
    )


def make_admission(tmp_path, *, project_root, session="manual-A",
                   root_session=None):
    """One neutral admission store with the session explicitly activated.
    Returns the admission SQLite path."""
    path = tmp_path / "admission.sqlite3"
    Admission(path).activate(session, project_root=str(project_root),
                             root_session=root_session)
    return path


def admission_token(path, session="manual-A"):
    return Admission(path).active_lease(session)["token"]
