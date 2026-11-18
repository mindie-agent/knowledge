"""[Role] Orthogonal behavioral regression tests for Group G2 (`knowledge-clean-arch`).

[Invariants]
- Exercises real subprocess cold imports, real `git worktree add` linked checkouts,
  real POSIX/Windows symlinks, real SQLite stores, and real PR checklist CLI parsing.
- Isolates all user/host environment paths; zero host pollution or mock-only self-assertions.

[Timeouts & Locks]
- Every case completes well under Tier 0/1 budgets (`< 5s`–`< 15s`).

[Side-Effect Policy]
- Writes exclusively inside pytest `tmp_path`.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from conftest import admission_token, make_admission, write_settings


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "iso-env"
    home = root / "home"
    cfg = root / "config"
    data = root / "data"
    cache = root / "cache"
    tmp = root / "tmp"
    for d in (home, cfg, data, cache, tmp):
        d.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(root / "appdata"),
            "LOCALAPPDATA": str(root / "localappdata"),
            "XDG_CONFIG_HOME": str(cfg),
            "XDG_DATA_HOME": str(data),
            "XDG_CACHE_HOME": str(cache),
            "MINDIE_CONFIG_HOME": str(cfg / "mindie"),
            "MINDIE_DATA_HOME": str(data / "mindie"),
            "MINDIE_DIAGNOSTICS_ROOT": str(root / "diag"),
            "REMOTE_DEV_AUTH_DIR": str(cfg / "remote-dev"),
            "UV_CACHE_DIR": str(cache / "uv"),
            "TMPDIR": str(tmp),
            "EMAIL": "mindie-test@localhost.invalid",
        }
    )
    return env


def test_lazy_top_level_import_fast_and_complete(tmp_path: Path) -> None:
    """Cold `import mindie_knowledge` loads zero submodules eagerly and resolves every `__all__` export."""
    probe = (
        "import json, sys, time\n"
        "t0 = time.perf_counter()\n"
        "import mindie_knowledge as mk\n"
        "cold_ms = (time.perf_counter() - t0) * 1000.0\n"
        "eager = sorted(m for m in sys.modules if m.startswith('mindie_knowledge.'))\n"
        "ver = mk.package_version()\n"
        "resolved = {name: getattr(mk, name) is not None for name in mk.__all__}\n"
        "print(json.dumps({'cold_ms': cold_ms, 'eager': eager, 'ver': ver, 'resolved': resolved}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=tmp_path,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert out["eager"] == []
    assert out["cold_ms"] < 50.0
    assert out["ver"]
    assert all(out["resolved"].values())


def test_rsi_modules_delegate_to_mindie_harness_when_installed_and_fallback_when_absent(tmp_path: Path) -> None:
    """RSI modules prefer `mindie_harness.rsi.*` when installed and fall back cleanly when absent."""
    fake_pkg = tmp_path / "fake_site"
    rsi_dir = fake_pkg / "mindie_harness" / "rsi"
    rsi_dir.mkdir(parents=True)
    (fake_pkg / "mindie_harness" / "__init__.py").write_text("", encoding="utf-8")
    (rsi_dir / "__init__.py").write_text("", encoding="utf-8")
    for mod_name, marker_attr in (
        ("rsi_loop", "HARNESS_RSI_LOOP_SENTINEL"),
        ("experiment_ledger", "HARNESS_LEDGER_SENTINEL"),
        ("rsi_crossover", "HARNESS_CROSSOVER_SENTINEL"),
    ):
        (rsi_dir / f"{mod_name}.py").write_text(
            f'__all__ = ["{marker_attr}"]\n{marker_attr} = "from-harness-{mod_name}"\n',
            encoding="utf-8",
        )

    env_with_harness = _isolated_env(tmp_path)
    repo_root = str(Path(__file__).resolve().parents[1])
    env_with_harness["PYTHONPATH"] = os.pathsep.join([str(fake_pkg), repo_root])
    delegated_script = (
        "import json\n"
        "from mindie_knowledge.graded import rsi_loop, experiment_ledger, rsi_crossover\n"
        "print(json.dumps({\n"
        "  'loop_using': rsi_loop.USING_HARNESS_RSI,\n"
        "  'loop_val': rsi_loop.HARNESS_RSI_LOOP_SENTINEL,\n"
        "  'ledger_using': experiment_ledger.USING_HARNESS_RSI,\n"
        "  'ledger_val': experiment_ledger.HARNESS_LEDGER_SENTINEL,\n"
        "  'cross_using': rsi_crossover.USING_HARNESS_RSI,\n"
        "  'cross_val': rsi_crossover.HARNESS_CROSSOVER_SENTINEL,\n"
        "}))\n"
    )
    res_delegated = subprocess.run(
        [sys.executable, "-c", delegated_script],
        cwd=tmp_path,
        env=env_with_harness,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    delegated = json.loads(res_delegated.stdout.strip())
    assert delegated == {
        "loop_using": True,
        "loop_val": "from-harness-rsi_loop",
        "ledger_using": True,
        "ledger_val": "from-harness-experiment_ledger",
        "cross_using": True,
        "cross_val": "from-harness-rsi_crossover",
    }

    # Verify real ImportError inside an installed mindie_harness.rsi module is NEVER swallowed.
    (rsi_dir / "rsi_loop.py").write_text("import non_existent_broken_dep_xyz\n", encoding="utf-8")
    broken_proc = subprocess.run(
        [sys.executable, "-c", "import mindie_knowledge.graded.rsi_loop"],
        cwd=tmp_path,
        env=env_with_harness,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert broken_proc.returncode != 0
    assert "non_existent_broken_dep_xyz" in broken_proc.stderr

    # Shadow RSI fallback bodies are retired; missing mindie_harness fails fast via ModuleNotFoundError.
    env_missing = _isolated_env(tmp_path / "missing")
    env_missing["PYTHONPATH"] = repo_root
    missing_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.abc, sys\n"
                "class _BlockHarness(importlib.abc.MetaPathFinder):\n"
                "    def find_spec(self, fullname, path, target=None):\n"
                "        if fullname == 'mindie_harness' or fullname.startswith('mindie_harness.'):\n"
                "            raise ModuleNotFoundError(\"No module named 'mindie_harness'\", name='mindie_harness')\n"
                "        return None\n"
                "sys.meta_path.insert(0, _BlockHarness())\n"
                "import mindie_knowledge.graded.rsi_loop\n"
            ),
        ],
        cwd=tmp_path,
        env=env_missing,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert missing_proc.returncode != 0
    assert "mindie_harness" in missing_proc.stderr


def test_runtime_inventory_v1_preserved_and_v2_substrate_wheel_supported(tmp_path: Path) -> None:
    """v1 `COMPONENTS` stays 9-entry invariant; `include_substrate=True` + wheel provenance handles `mindie-harness`."""
    from mindie_knowledge import component_releases, component_runtime, runtime_inventory

    assert "mindie-harness" not in runtime_inventory.COMPONENTS
    assert len(runtime_inventory.COMPONENTS) == 9
    assert runtime_inventory.COMPONENTS_V2["mindie-harness"] == "mindie-agent/harness"
    assert component_releases.component_registry(include_substrate=False) == runtime_inventory.COMPONENTS
    assert component_releases.component_registry(include_substrate=True) == runtime_inventory.COMPONENTS_V2

    sha = "a" * 40
    target_spec = {"repository": "mindie-agent/harness", "version": "0.1.0", "revision": sha}
    with pytest.raises(ValueError, match="unsupported component"):
        runtime_inventory.validate_targets({"mindie-harness": target_spec})

    validated_v2 = runtime_inventory.validate_targets(
        {"mindie-harness": target_spec},
        include_substrate=True,
    )
    assert validated_v2["mindie-harness"]["repository"] == "mindie-agent/harness"

    fake_site = tmp_path / "site"
    dist_info = fake_site / "mindie_harness-0.1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: mindie-harness\nVersion: 0.1.0\n", encoding="utf-8")
    (dist_info / "direct_url.json").write_text(
        json.dumps(
            {
                "url": "https://github.com/mindie-agent/harness/releases/download/v0.1.0/mindie_harness-0.1.0-py3-none-any.whl",
                "archive_info": {"hashes": {"sha256": "b" * 64}},
            }
        ),
        encoding="utf-8",
    )
    import importlib.metadata as im

    real_dist = im.distribution

    def _fake_distribution(name: str):
        if name == "mindie-harness":
            return im.PathDistribution(dist_info)
        raise im.PackageNotFoundError(name)

    im.distribution = _fake_distribution  # type: ignore[assignment]
    try:
        assert runtime_inventory.inventory(include_substrate=False) == {}
        with pytest.raises(ValueError, match="no official immutable installation receipt"):
            runtime_inventory.inventory(include_substrate=True, allow_wheel_provenance=False)
        inv_v2 = runtime_inventory.inventory(include_substrate=True, allow_wheel_provenance=True)
        entry = inv_v2["mindie-harness"]
        assert entry["version"] == "0.1.0"
        assert entry["provenance"] == "wheel"
        assert entry["archive_sha256"] == "b" * 64
    finally:
        im.distribution = real_dist  # type: ignore[assignment]

    src_dir = tmp_path / "plugin-src"
    src_dir.mkdir()
    (src_dir / "runtime-requirements.txt").write_text(
        f"mindie-knowledge @ git+https://github.com/mindie-agent/knowledge@{sha}\n"
        f"mindie-harness @ git+https://github.com/mindie-agent/harness@{sha}\n",
        encoding="utf-8",
    )
    baseline_v1 = component_runtime.source_baseline(src_dir, include_substrate=False)
    assert set(baseline_v1["runtime"]) == {"mindie-knowledge"}
    baseline_v2 = component_runtime.source_baseline(src_dir, include_substrate=True)
    assert baseline_v2["runtime"]["mindie-harness"] == sha


def test_linked_git_worktree_supported_across_git_layout_publication_and_gitops(tmp_path: Path) -> None:
    """Linked Git worktrees where `.git` is a file (`gitdir: ...`) work seamlessly across all 3 callers."""
    from mindie_knowledge import publication_check
    from mindie_knowledge.community import gitops
    from mindie_knowledge.community.transport import FileTransport
    from mindie_knowledge.git_layout import resolve_git_dir

    env = _isolated_env(tmp_path)
    origin_repo = tmp_path / "origin-repo"
    origin_repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(origin_repo)], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(origin_repo), "config", "user.name", "Test"], env=env, check=True)
    subprocess.run(["git", "-C", str(origin_repo), "config", "user.email", "test@localhost.invalid"], env=env, check=True)
    (origin_repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(origin_repo), "add", "README.md"], env=env, check=True)
    subprocess.run(["git", "-C", str(origin_repo), "commit", "-m", "init"], env=env, check=True, capture_output=True)

    linked_wt = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "-C", str(origin_repo), "worktree", "add", "-b", "wt-branch", str(linked_wt), "main"],
        env=env,
        check=True,
        capture_output=True,
    )
    assert (linked_wt / ".git").is_file()
    assert not (linked_wt / ".git").is_dir()

    resolved = resolve_git_dir(linked_wt)
    assert resolved is not None and resolved.is_dir()

    state_path = publication_check._default_state_path(linked_wt)
    assert state_path == resolved / "mindie-validator-state.json"

    remote_url = subprocess.run(
        ["git", "-C", str(origin_repo), "rev-parse", "--show-toplevel"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(linked_wt), "remote", "add", "origin", remote_url], env=env, check=True)
    cloned_wt = gitops.ensure_clone(remote_url, linked_wt, gitops.Deadline(5.0), env=env)
    assert os.path.samefile(cloned_wt, linked_wt)

    bare_remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(origin_repo), str(bare_remote)], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(origin_repo), "remote", "add", "bare", str(bare_remote)], env=env, check=True)
    subprocess.run(["git", "-C", str(origin_repo), "checkout", "-b", "contrib-pr"], env=env, check=True, capture_output=True)
    (origin_repo / "note.md").write_text("from contributor\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(origin_repo), "add", "note.md"], env=env, check=True)
    subprocess.run(["git", "-C", str(origin_repo), "commit", "-m", "add note"], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(origin_repo), "push", "bare", "contrib-pr:refs/heads/contrib-pr"], env=env, check=True, capture_output=True)

    merge_parent = tmp_path / "merge-parent"
    subprocess.run(["git", "clone", str(bare_remote), str(merge_parent)], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(merge_parent), "checkout", "--detach", "HEAD"], env=env, check=True, capture_output=True)
    state_file = tmp_path / "dev-transport" / "state.json"
    merge_work = state_file.parent / "merge-work" / "mindie-agent_knowledge-vllm-ascend"
    merge_work.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(merge_parent), "worktree", "add", "--detach", str(merge_work), "HEAD"],
        env=env,
        check=True,
        capture_output=True,
    )
    assert (merge_work / ".git").is_file()

    transport = FileTransport(state_file, remotes={"mindie-agent/knowledge-vllm-ascend": str(bare_remote)})
    merged_sha = transport._merge_into_remote(
        "mindie-agent/knowledge-vllm-ascend",
        {"base": {"ref": "main"}, "head": {"ref": "contrib-pr"}},
        gitops.Deadline(10.0),
    )
    assert len(merged_sha) == 40
    assert (merge_work / "note.md").read_text(encoding="utf-8") == "from contributor\n"


def test_file_source_allows_in_root_symlinks_and_rejects_escaping_symlinks(tmp_path: Path) -> None:
    """`file_source` (`checked_bytes` & `FileContents`) reads symlinks inside `root` and rejects escaping symlinks."""
    from mindie_knowledge.materials.file_source import FileContents, checked_bytes

    corpus_root = tmp_path / "corpus"
    shared_dir = corpus_root / "_shared"
    unit_dir = corpus_root / "units"
    shared_dir.mkdir(parents=True)
    unit_dir.mkdir(parents=True)

    real_inside = shared_dir / "axiom.md"
    real_inside.write_text("# Inside Root\n", encoding="utf-8", newline="\n")
    outside_secret = tmp_path / "outside-secret.md"
    outside_secret.write_text("TOP SECRET OUTSIDE ROOT\n", encoding="utf-8")

    in_link = unit_dir / "linked-axiom.md"
    out_link = unit_dir / "escaped-secret.md"
    in_link.symlink_to(real_inside)
    out_link.symlink_to(outside_secret)

    assert checked_bytes(in_link, corpus_root) == b"# Inside Root\n"
    fc = FileContents.from_paths(corpus_root, ["units/linked-axiom.md"])
    assert fc["units/linked-axiom.md"] == "# Inside Root\n"

    with pytest.raises(ValueError, match="within its bound root"):
        checked_bytes(out_link, corpus_root)
    with pytest.raises(ValueError, match="within its bound root"):
        FileContents.from_paths(corpus_root, ["units/escaped-secret.md"])


def test_query_audit_never_pollutes_arbitrary_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`resolve_workspace_audit_paths` never writes `knowledge_query_audit.jsonl` into an unowned `Path.cwd()`."""
    from mindie_knowledge.graded.explore_support import resolve_workspace_audit_paths

    unowned_cwd = tmp_path / "unowned-user-repo"
    unowned_cwd.mkdir()
    monkeypatch.chdir(unowned_cwd)
    monkeypatch.delenv("MINDIE_BENCH_WORKSPACE", raising=False)

    scope, read_paths, write_paths = resolve_workspace_audit_paths(unowned_cwd, persist_root=False)
    assert Path(scope) == unowned_cwd.resolve()
    assert read_paths == []
    assert write_paths == []

    # When persist_root=True without MINDIE_BENCH_WORKSPACE, writes route strictly to MINDIE_DATA_HOME/query-audit.
    owned_data = tmp_path / "owned-data-home"
    monkeypatch.setenv("MINDIE_DATA_HOME", str(owned_data))
    _, read_gated, write_gated = resolve_workspace_audit_paths(unowned_cwd, persist_root=True)
    assert len(write_gated) == 1
    assert owned_data.resolve() in write_gated[0].parents
    assert not any(unowned_cwd.resolve() in p.parents for p in write_gated)
    assert not (unowned_cwd / "logs").exists()


def test_stop_capture_enforces_single_outer_deadline(tmp_path: Path, store) -> None:
    """`accept_stop` enforces one monotonic outer deadline and degrades honestly when budget expires."""
    from lane_support import parser_path
    from material_worker_fixture import command as summary_command
    from mindie_knowledge.loop.handoff import accept_stop
    from mindie_knowledge.loop.transcript_redaction import install_scanner

    project = tmp_path / "proj"
    project.mkdir()
    transcript = project / "turn.jsonl"
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
    settings_path = tmp_path / "community.json"
    write_settings(settings_path, enabled=True, roots=[str(project)], consent_choice="contribute")
    adm_path = make_admission(tmp_path, project_root=project, session="sess-deadline")
    tok = admission_token(adm_path, session="sess-deadline")

    cfg_path = tmp_path / "domain.json"
    cfg_path.write_text(
        json.dumps(
            {
                "root": str(tmp_path),
                "domain": "vllm-ascend",
                "community_config": str(settings_path),
                "admission_path": str(adm_path),
                "capture_mode": "public-transcript",
                "redactor_executable": install_scanner(),
                "summary_command": summary_command(),
                "transcript_adapter": str(parser_path("codex")),
            }
        ),
        encoding="utf-8",
    )

    expired_deadline = time.monotonic() - 0.01
    result = accept_stop(
        cfg_path,
        {
            "hook_event_name": "Stop",
            "harness": "codex",
            "identity_kind": "turn",
            "session_id": "sess-deadline",
            "turn_id": "turn-1",
            "mindie_activation": tok,
            "transcript_path": str(transcript),
        },
        deadline=expired_deadline,
    )
    assert result["stage"] == "unavailable"
    assert result["cause"] == "budget-exhausted"


def test_state_service_pr_checklist_validator() -> None:
    """`.github/scripts/check_pr_checklist.py --profile state-service` enforces concrete per-ID or summary evidence."""
    script_path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_pr_checklist.py"
    spec = importlib.util.spec_from_file_location("check_pr_checklist", script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    valid_body = """
## 变更摘要 (Summary)
- Convert RSI modules to `mindie_harness.rsi` bridges and enforce single `Stop` deadline.

## 设计原则 Review (Pre-PR Checklist — `state-service`)
- [x] **SVC-CONSENT** — Consent gate verified
- [x] **SVC-STATE-COMPAT** — Schema compatibility verified
- [x] **SVC-LIFETIME** — Deadline and symlink boundaries verified
- [x] **SVC-EXPORT** — Outbox reconciliation verified

## 自查证据 (Evidence)
- **SVC-CONSENT**: `tests/test_consent_store.py` passed; `choice_revision` untouched.
- **SVC-STATE-COMPAT**: `state-v4.sqlite3` and `DATA_FORMAT_V1` preserved 100%.
- **SVC-LIFETIME**: `accept_stop` shares single monotonic deadline; all modules < 800 lines.
- **SVC-EXPORT**: not-applicable: no changes to community GitHub PR transport payload format.

## 验证结果 (Verification)
- Ran `pytest -q tests/test_clean_arch_g2.py` (all passed).
"""
    assert mod.validate_pr_body(valid_body, profile="state-service") == []

    bad_body = valid_body.replace(
        "not-applicable: no changes to community GitHub PR transport payload format.",
        "N/A",
    )
    errs = mod.validate_pr_body(bad_body, profile="state-service")
    assert any("SVC-EXPORT" in e for e in errs)
