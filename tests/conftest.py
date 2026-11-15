"""[Role] Shared pytest fixtures and host-isolation setup for the knowledge test-suite.

[Invariants] Isolation is applied at import before product modules resolve ``Path.home``
or diagnostics policy, never inside pytest ``--basetemp`` (which pytest wipes after import).
[Timeouts & Locks] In-memory and local temporary directory setup; no network or locks.
[Side-Effect Policy] Redirects ``HOME`` and ``MINDIE_*`` roots into ``ISOLATION_ROOT``;
keeps reporting disabled.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lane_support import PARSER_NAMES, parser_path
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

def _safe_mkdtemp_parent():
    """Avoid placing ISOLATION_ROOT inside pytest --basetemp, which pytest wipes after conftest import."""
    basetemp = None
    for idx, arg in enumerate(sys.argv[1:], start=1):
        if arg.startswith("--basetemp="):
            basetemp = Path(arg.split("=", 1)[1]).expanduser().resolve()
            break
        if arg == "--basetemp" and idx + 1 < len(sys.argv):
            basetemp = Path(sys.argv[idx + 1]).expanduser().resolve()
            break
    if os.name == "nt":
        runner_tmp = os.environ.get("RUNNER_TEMP", "").strip()
        if runner_tmp:
            rt = Path(runner_tmp).expanduser().resolve()
            if rt.is_dir() and (basetemp is None or (rt != basetemp and basetemp not in rt.parents)):
                return str(rt)
    tmpdir = Path(tempfile.gettempdir()).expanduser().resolve()
    if basetemp is not None and (tmpdir == basetemp or basetemp in tmpdir.parents):
        parent = basetemp.parent
        parent.mkdir(parents=True, exist_ok=True)
        return str(parent)
    return None


def _drop_linux_root_dac_override():
    """Ensure chmod(0) unreadable-file tests raise PermissionError even when shards run as uid 0."""
    if sys.platform != "linux" or os.geteuid() != 0:
        return
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        for cap in (1, 2):  # CAP_DAC_OVERRIDE=1, CAP_DAC_READ_SEARCH=2
            libc.prctl(24, cap, 0, 0, 0)  # PR_CAPBSET_DROP=24

        class _CapHeader(ctypes.Structure):
            _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

        class _CapData(ctypes.Structure):
            _fields_ = [
                ("effective", ctypes.c_uint32),
                ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32),
            ]

        hdr = _CapHeader(0x20080522, 0)
        data = (_CapData * 2)()
        if libc.capget(ctypes.byref(hdr), data) == 0:
            mask = ~((1 << 1) | (1 << 2))
            data[0].effective &= mask
            data[0].permitted &= mask
            data[0].inheritable &= mask
            libc.capset(ctypes.byref(hdr), data)
    except TimeoutError:
        raise
    except OSError:
        pass


_drop_linux_root_dac_override()

_explicit_root = os.environ.get("_MINDIE_KNOWLEDGE_BASE_TEST_ROOT") or os.environ.get("MINDIE_KNOWLEDGE_TEST_ROOT")
if _explicit_root:
    _base_root = Path(_explicit_root).expanduser().resolve()
else:
    _base_root = Path(tempfile.mkdtemp(prefix="mk-iso-", dir=_safe_mkdtemp_parent()))
os.environ["_MINDIE_KNOWLEDGE_BASE_TEST_ROOT"] = str(_base_root)
_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER", "").strip()
if _xdist_worker:
    _prev_worker_pid = os.environ.get("_MINDIE_TEST_WORKER_PID", "")
    if _prev_worker_pid and _prev_worker_pid != str(os.getpid()):
        ISOLATION_ROOT = Path(tempfile.mkdtemp(prefix=f"s-{_xdist_worker}-", dir=_base_root))
    else:
        ISOLATION_ROOT = _base_root / _xdist_worker
    os.environ["_MINDIE_TEST_WORKER_PID"] = str(os.getpid())
else:
    ISOLATION_ROOT = _base_root
os.environ["MINDIE_KNOWLEDGE_TEST_ROOT"] = str(ISOLATION_ROOT)
REAL_HOME = Path(os.environ.get("_MINDIE_TEST_REAL_HOME") or os.environ.get("HOME") or str(Path.home()))
os.environ["_MINDIE_TEST_REAL_HOME"] = str(REAL_HOME)
_WATCH = [
    REAL_HOME / ".config" / "mindie-agent" / "diagnostics.json",
    REAL_HOME / ".local" / "state" / "mindie" / "diagnostics",
    REAL_HOME / ".local" / "share" / "mindie-diagnostics",
    REAL_HOME / ".config" / "mindie-diagnostics",
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


def _isolated_home(root):
    """Build every per-user location products may resolve from the environment."""
    home = root / "home"
    config = root / "config"
    state = root / "state"
    data = root / "data"
    local_appdata = root / "local-appdata"
    appdata = root / "appdata"
    diagnostics = root / "diagnostics"
    for directory in (home, config, state, data, local_appdata, appdata, diagnostics):
        directory.mkdir(parents=True, exist_ok=True)
    return home, config, state, data, local_appdata, appdata, diagnostics


_home, _config, _state, _data, _local_appdata, _appdata, _diag_root = _isolated_home(ISOLATION_ROOT)
_real_gitleaks = REAL_HOME / ".cache" / "mindie-agent" / "gitleaks"
if not _real_gitleaks.is_dir():
    for _alt_gitleaks in (
        Path(os.environ.get("MINDIE_GITLEAKS_CACHE", "")) if os.environ.get("MINDIE_GITLEAKS_CACHE") else None,
        Path("/dev/shm/agy-gitleaks-cache"),
    ):
        if _alt_gitleaks is not None and _alt_gitleaks.is_dir():
            _real_gitleaks = _alt_gitleaks
            break
_isolated_gitleaks = _home / ".cache" / "mindie-agent" / "gitleaks"
if _real_gitleaks.is_dir() and not _isolated_gitleaks.exists():
    import shutil as _shutil
    _isolated_gitleaks.parent.mkdir(parents=True, exist_ok=True)
    _shutil.copytree(_real_gitleaks, _isolated_gitleaks)
os.environ.setdefault("MINDIE_DISABLE_FSYNC", "1")
# Prevent Git reflog/receive-pack ident_default_email() from blocking 5s on DNS
# when HOME/.gitconfig is redirected to an empty isolated test directory.
os.environ.setdefault("EMAIL", "mindie-test@localhost.invalid")
os.environ["HOME"] = str(_home)
os.environ["USERPROFILE"] = str(_home)
os.environ["XDG_CONFIG_HOME"] = str(_config)
os.environ["XDG_STATE_HOME"] = str(_state)
os.environ["XDG_DATA_HOME"] = str(_data)
os.environ["MINDIE_CONFIG_HOME"] = str(_config)
os.environ["MINDIE_DATA_HOME"] = str(_data)
os.environ["LOCALAPPDATA"] = str(_local_appdata)
os.environ["APPDATA"] = str(_appdata)
_TEST_DIAGNOSTICS_ROOT = _diag_root
os.environ["MINDIE_DIAGNOSTICS_ROOT"] = str(_TEST_DIAGNOSTICS_ROOT)
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


_AUTO_BASETEMP_ROOT: Path | None = None


def pytest_sessionfinish(session, exitstatus):
    """Leave no synthetic diagnostics tree behind, even after a failed run."""
    import shutil
    import stat

    shutil.rmtree(_TEST_DIAGNOSTICS_ROOT, ignore_errors=True)
    if _AUTO_BASETEMP_ROOT is not None:
        def _writable(func, target, _exc):
            try:
                os.chmod(target, stat.S_IWRITE)
                func(target)
            except TimeoutError:
                raise
            except OSError:
                pass

        shutil.rmtree(_AUTO_BASETEMP_ROOT, onerror=_writable)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    global _AUTO_BASETEMP_ROOT
    if os.name == "nt" and not config.option.basetemp and not hasattr(config, "workerinput"):
        runner_tmp = os.environ.get("RUNNER_TEMP", "").strip()
        short_parent = runner_tmp if runner_tmp and Path(runner_tmp).is_dir() else None
        _AUTO_BASETEMP_ROOT = Path(tempfile.mkdtemp(prefix="mkb-", dir=short_parent))
        config.option.basetemp = str(_AUTO_BASETEMP_ROOT / "bt")
    config.addinivalue_line("markers", "benchmark: large payload throughput benchmark")
    config.addinivalue_line("markers", "live_npu: requires standing A2/A3 SSH access")
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
