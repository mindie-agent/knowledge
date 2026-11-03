"""Experiment ledger judgment, source fingerprint, dedup and budget are code-enforced.

Ported from mindie-agent-codex tests/test_experiment_ledger.py (PR #83, a326b7b).
"""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from mindie_knowledge.graded import experiment as ledger
from mindie_knowledge.loop.locks import StartLock

MODULE = "mindie_knowledge.graded.experiment"


def trials(*values, status="completed"):
    return [dict(status=status, metric=v) for v in values]


def evidence(**changes):
    body = dict(repetitions=2, direction="maximize", target_speedup=1.05,
                baseline=trials(100.0, 100.0), candidate=trials(120.0, 118.0),
                correctness="passed", baseline_source_digest="sha256:a",
                source_digest="sha256:b")
    body.update(changes)
    return body


def record(**changes):
    body = dict(task_id="t1", exp_id="e1", hypothesis="fuse rmsnorm", run_ids=["r1"],
                source_digest="sha256:b", judgment="supported", run_status="completed",
                scope_conditions=dict(soc="910B"), card_seconds=60)
    body.update(changes)
    return body


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   timeout=15)


def test_complete_gain_is_supported_by_engine_gate():
    result = ledger.judge(evidence())
    assert result["judgment"] == "supported", result
    assert result["engine_verdict"] in ("distill_candidate", "retain_verified_evidence")
    assert result["speedup"] == pytest.approx(1.19)


def test_valid_gain_below_target_is_supported_without_goal():
    # 1.03x is a real gain under a 1.05x target: kept, not refuted (three axes).
    result = ledger.judge(evidence(candidate=trials(103.0, 103.0)))
    assert result["judgment"] == "supported" and result["outcome"] == "subtarget_valid"
    assert result["axes"]["evidence_valid"] and not result["axes"]["goal_attained"]


def test_ratio_inside_noise_is_inconclusive():
    result = ledger.judge(evidence(candidate=trials(100.5, 100.0)))
    assert result["judgment"] == "inconclusive" and result["outcome"] == "speedup_within_noise"
    wider = ledger.judge(evidence(candidate=trials(103.0, 103.0), noise_margin=0.05))
    assert wider["judgment"] == "inconclusive"


def test_regression_beyond_noise_is_refuted():
    result = ledger.judge(evidence(candidate=trials(90.0, 92.0)))
    assert result["judgment"] == "refuted" and result["outcome"] == "regression"


def test_g1_g3_evidence_reaches_the_engine_gates():
    failed_ablation = ledger.judge(evidence(ablation_ok=False))
    assert failed_ablation["judgment"] == "supported"
    assert failed_ablation["engine_gates"]["G3"] is False
    assert failed_ablation["axes"]["reusable"] is False
    clean = ledger.judge(evidence(ablation_ok=True, workspace_delta_bytes=0))
    assert clean["engine_gates"]["G3"] is True and clean["axes"]["reusable"] is True


def test_missing_evidence_outranks_a_failed_gate():
    # Correctness failed AND a trial crashed: missing evidence wins.
    result = ledger.judge(evidence(correctness="failed",
                                   candidate=trials(120.0) + trials(None, status="infra_failed")))
    assert result["judgment"] == "inconclusive"
    assert any("infra_failed" in r for r in result["reasons"])


@pytest.mark.parametrize("changes", [
    dict(compile_ok=False),
    dict(correctness=None),
    dict(source_digest=None),
    dict(source_digest="sha256:a"),
    dict(candidate=trials(120.0)),
])
def test_incomplete_evidence_is_inconclusive_not_refuted(changes):
    assert ledger.judge(evidence(**changes))["judgment"] == "inconclusive"


def test_minimize_direction_inverts_ratio():
    result = ledger.judge(evidence(direction="minimize", baseline=trials(10.0, 10.0),
                                   candidate=trials(8.0, 8.0)))
    assert result["judgment"] == "supported"
    assert result["speedup"] == pytest.approx(1.25)


def test_append_rejects_infra_failure_recorded_as_refuted(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    with pytest.raises(ValueError, match="inconclusive"):
        ledger.append(path, record(run_status="infra_failed", judgment="refuted"))
    with pytest.raises(ValueError, match="source_digest"):
        ledger.append(path, record(source_digest=""))
    assert not path.exists()


def test_append_is_one_lf_line_and_rejects_silent_rewrite(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record())
    with pytest.raises(ValueError, match="supersedes_exp_id"):
        ledger.append(path, record(judgment="refuted"))
    ledger.append(path, record(exp_id="e2", supersedes_exp_id="e1", judgment="refuted"))
    raw = path.read_bytes()
    assert raw.count(b"\n") == 2 and b"\r" not in raw
    assert [r["schema"] for r in ledger.read_ledger(path)] == [ledger.SCHEMA] * 2


def test_gate_blocks_duplicate_source_but_retries_inconclusive(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(exp_id="e0", source_digest="sha256:x",
                               judgment="inconclusive", run_status="infra_failed"))
    assert ledger.gate(path, task_id="t1", source_digest="sha256:x")["decision"] == "submit"
    ledger.append(path, record())
    dup = ledger.gate(path, task_id="t1", source_digest="sha256:b", scope=dict(soc="910B"))
    assert dup["decision"] == "duplicate" and dup["prior"]["exp_id"] == "e1"
    other_scope = ledger.gate(path, task_id="t1", source_digest="sha256:b",
                              scope=dict(soc="910C"))
    assert other_scope["decision"] == "submit"
    assert ledger.gate(path, task_id="t2", source_digest="sha256:b")["decision"] == "submit"


def test_gate_enforces_run_and_card_second_budget(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(run_ids=["r1", "r2"], card_seconds=90))
    assert ledger.gate(path, task_id="t1", source_digest="sha256:new",
                       max_runs=2)["decision"] == "budget_exhausted"
    assert ledger.gate(path, task_id="t1", source_digest="sha256:new",
                       max_card_seconds=90)["decision"] == "budget_exhausted"
    assert ledger.gate(path, task_id="t1", source_digest="sha256:new", max_runs=3,
                       max_card_seconds=120)["decision"] == "submit"


def test_fingerprint_changes_with_tracked_and_untracked_edits(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "base")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    first = ledger.fingerprint(repo)
    assert first["dirty"] is True and first["untracked_files"] == 1
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8", newline="\n")
    second = ledger.fingerprint(repo)
    assert second["source_digest"] != first["source_digest"]
    assert ledger.fingerprint(repo)["source_digest"] == second["source_digest"]
    (repo / "pkg").mkdir()
    with pytest.raises(ValueError, match="root"):
        ledger.fingerprint(repo / "pkg")


def test_cli_exit_codes_distinguish_blocked_from_error(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    cli = [sys.executable, "-I", "-m", MODULE]
    appended = subprocess.run([*cli, "append", "--ledger", str(path)],
                              input=json.dumps(record()), capture_output=True, text=True,
                              timeout=5)
    assert appended.returncode == 0, appended.stderr
    blocked = subprocess.run([*cli, "gate", "--ledger", str(path),
                              "--task-id", "t1", "--source-digest", "sha256:b",
                              "--scope", json.dumps(dict(soc="910B"))],
                             capture_output=True, text=True, timeout=5)
    assert blocked.returncode == ledger.BLOCKED
    assert json.loads(blocked.stdout)["decision"] == "duplicate"
    broken = subprocess.run([*cli, "judge"], input="{}",
                            capture_output=True, text=True, timeout=5)
    assert broken.returncode == 1 and json.loads(broken.stdout)["status"] == "error"


def test_missing_scope_matches_only_an_unscoped_record(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record())  # scoped to 910B
    assert ledger.gate(path, task_id="t1", source_digest="sha256:b")["decision"] == "submit"
    ledger.append(path, record(exp_id="e2", source_digest="sha256:c", scope_conditions=None))
    scoped = ledger.gate(path, task_id="t1", source_digest="sha256:c", scope={})
    assert scoped["decision"] == "duplicate"
    assert ledger.gate(path, task_id="t1", source_digest="sha256:c")["decision"] == "duplicate"


def test_superseding_revision_is_not_charged_twice(tmp_path):
    path = tmp_path / "experiment-ledger.jsonl"
    ledger.append(path, record(run_ids=["r1", "r2"], card_seconds=60))
    # Same runs, corrected judgment and card time: replaces, does not add.
    ledger.append(path, record(run_ids=["r1", "r2"], card_seconds=50, judgment="refuted",
                               supersedes_exp_id="e1"))
    spent = ledger.gate(path, task_id="t1", source_digest="sha256:z")["spent"]
    assert spent["runs"] == 2 and spent["card_seconds"] == 50
    # A revision that adds a rerun adds its time.
    ledger.append(path, record(run_ids=["r1", "r2", "r3"], card_seconds=30,
                               supersedes_exp_id="e1"))
    spent = ledger.gate(path, task_id="t1", source_digest="sha256:z")["spent"]
    assert spent["runs"] == 3 and spent["card_seconds"] == 80
    live, _ = ledger.fold(ledger.read_ledger(path))
    assert len(live) == 1 and live[0]["run_ids"] == ["r1", "r2", "r3"]
    assert ledger.gate(path, task_id="t1", source_digest="sha256:b",
                       scope=dict(soc="910B"))["prior"]["judgment"] == "supported"


def test_large_untracked_file_is_identified_by_size(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    (repo / "dump.bin").write_bytes(b"0" * 64)
    monkeypatch.setattr(ledger, "LARGE_FILE_BYTES", 16)
    first = ledger.fingerprint(repo)
    assert first["unhashed_large_files"] == ["dump.bin"]
    (repo / "dump.bin").write_bytes(b"1" * 64)  # same size: same identity
    assert ledger.fingerprint(repo)["source_digest"] == first["source_digest"]
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert ledger.fingerprint(repo)["source_digest"] != first["source_digest"]


def test_append_waits_a_bounded_time_for_a_held_lock(tmp_path, monkeypatch):
    path = tmp_path / "experiment-ledger.jsonl"
    monkeypatch.setattr(ledger, "LOCK_WAIT_SECONDS", 0.2)
    monkeypatch.delenv("MINDIE_START_LOCK_WAIT_SECONDS", raising=False)
    with StartLock(str(path) + ".lock"):
        with pytest.raises(TimeoutError, match="locked by another writer"):
            ledger.append(path, record())
    assert not path.exists()
    ledger.append(path, record())
    assert len(ledger.read_ledger(path)) == 1
