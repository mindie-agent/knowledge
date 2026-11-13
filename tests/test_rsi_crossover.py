"""Tests for multi-parent crossover DAG lineage and parent-reused Gate-3 ablation.

[Role]
  Verify orthogonal sibling crossover suggestion, automatic Leave-One-Out Gate-3
  ablation synthesis from single-parent ledger runs, rejection of non-additive
  dead-factor combinations, and strict cumulative speedup honesty.

[Invariants]
  * Isolates HOME, MINDIE_CONFIG_HOME, MINDIE_DATA_HOME, and MINDIE_DIAGNOSTICS_ROOT
    in every test (Principle 16).
  * Cumulative speedup never synthetically multiplies sibling ratios.

[Timeouts & Locks]
  Tier 1 local git + JSONL ledger tests (< 10s).

[Side-Effect Policy]
  Writes only under pytest ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import pytest

from mindie_knowledge.graded import experiment_ledger as ledger
from mindie_knowledge.graded import rsi_loop
from mindie_knowledge.graded.rsi_crossover import suggest_orthogonal_crossovers


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HOME", "MINDIE_CONFIG_HOME", "MINDIE_DATA_HOME", "MINDIE_DIAGNOSTICS_ROOT"):
        d = tmp_path / name.lower()
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(d))


def _init_git_repo(path: Path, content: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@mindie.local"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "MindIE Test"], cwd=path, check=True)
    (path / "kernel.py").write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "kernel.py"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def test_orthogonal_subtarget_siblings_combine_into_supported_crossover(tmp_path: Path) -> None:
    base_repo = _init_git_repo(tmp_path / "repo_base", "def run():\n    return 1\n")
    cand_a = _init_git_repo(tmp_path / "repo_a", "def run():\n    return 'attn_opt'\n")
    cand_b = _init_git_repo(tmp_path / "repo_b", "def run():\n    return 'norm_opt'\n")
    cand_ab = _init_git_repo(tmp_path / "repo_ab", "def run():\n    return 'attn_and_norm_opt'\n")
    cand_dead = _init_git_repo(tmp_path / "repo_dead", "def run():\n    return 'dead_combo'\n")

    state_path = tmp_path / "rsi-loop.json"
    ledger_path = tmp_path / "experiment-ledger.jsonl"

    rsi_loop.init(
        state_path,
        task_id="qwen3_decode_opt",
        ledger_path=ledger_path,
        baseline_repo=base_repo,
        direction="maximize",
        repetitions=2,
        target_speedup=1.05,
    )

    # Sibling A: +3.0% on Attention (subtarget_valid, < 1.05 target)
    res_a, code_a = rsi_loop.complete(
        state_path,
        "record",
        {
            "bottleneck_summary": {"top_block": "Attention"},
            "profile_ref": "prof_0.json",
            "unit_refs": [],
            "skipped_reason": "direct profiling evidence",
            "exp_id": "exp_attn",
            "hypothesis": "tile QKV FlashAttention prefetch",
            "candidate_repo": str(cand_a),
            "factors": ["attn_prefetch"],
            "bottleneck_lane": "Attention",
            "run_ids": ["run_a1"],
            "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
            "candidate": [{"status": "completed", "metric": 103.0}, {"status": "completed", "metric": 103.0}],
            "correctness": "passed",
            "compile_ok": True,
            "card_seconds": 12.0,
        },
    )
    assert code_a == 0
    assert res_a["entry"]["verdict"] == "subtarget_valid"

    # Sibling B: anchored to initial baseline B0, +3.2% on Norm (subtarget_valid)
    res_b, code_b = rsi_loop.complete(
        state_path,
        "record",
        {
            "bottleneck_summary": {"top_block": "Norm"},
            "profile_ref": "prof_0.json",
            "unit_refs": [],
            "skipped_reason": "direct profiling evidence",
            "exp_id": "exp_norm",
            "anchor_baseline_exp_id": "initial",
            "hypothesis": "fuse residual into RMSNorm",
            "candidate_repo": str(cand_b),
            "factors": ["rmsnorm_residual_fuse"],
            "bottleneck_lane": "Norm",
            "run_ids": ["run_b1"],
            "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
            "candidate": [{"status": "completed", "metric": 103.2}, {"status": "completed", "metric": 103.2}],
            "correctness": "passed",
            "compile_ok": True,
            "card_seconds": 12.0,
        },
    )
    assert code_b == 0
    assert res_b["entry"]["verdict"] == "subtarget_valid"

    # Advance to consult/propose and check automatic crossover discovery
    rsi_loop.complete(
        state_path,
        "profile",
        {"bottleneck_summary": {"top_block": "Attention+Norm"}, "profile_ref": "prof_1.json"},
    )
    step_view = rsi_loop.next_step(state_path)
    assert "crossover_candidates" in step_view
    top_cross = step_view["crossover_candidates"][0]
    assert top_cross["parent_exp_ids"] == ["exp_attn", "exp_norm"]
    assert top_cross["independent_estimate_ratio"] == round(1.03 * 1.032, 6)

    # First test a non-additive crossover (1.035x <= 1.032 + 0.01 noise_margin -> Gate 3 prunes!)
    res_dead, code_dead = rsi_loop.complete(
        state_path,
        "record",
        {
            "unit_refs": [],
            "skipped_reason": "testing crossover",
            "exp_id": "exp_cross_dead",
            "anchor_baseline_exp_id": "initial",
            "parent_exp_ids": ["exp_attn", "exp_norm"],
            "hypothesis": "conflicting attn + norm merge",
            "candidate_repo": str(cand_dead),
            "factors": ["attn_prefetch", "rmsnorm_residual_fuse"],
            "run_ids": ["run_dead1"],
            "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
            "candidate": [{"status": "completed", "metric": 103.5}, {"status": "completed", "metric": 103.5}],
            "correctness": "passed",
            "compile_ok": True,
            "card_seconds": 12.0,
        },
    )
    assert code_dead == 0
    assert res_dead["entry"]["verdict"] == "refuted"
    assert res_dead["entry"]["outcome"] == "ablation_not_retained"

    # Now record genuinely synergistic crossover (+6.2% vs B0 -> passes Gate 3 automatically & hits >=1.05 supported!)
    res_ab, code_ab = rsi_loop.complete(
        state_path,
        "record",
        {
            "bottleneck_summary": {"top_block": "Attention+Norm"},
            "profile_ref": "prof_1.json",
            "unit_refs": [],
            "skipped_reason": "orthogonal crossover",
            "exp_id": "exp_cross_ok",
            "anchor_baseline_exp_id": "initial",
            "parent_exp_ids": ["exp_attn", "exp_norm"],
            "hypothesis": "combine orthogonal FlashAttention prefetch and fused RMSNorm",
            "candidate_repo": str(cand_ab),
            "factors": ["attn_prefetch", "rmsnorm_residual_fuse"],
            "run_ids": ["run_ab1"],
            "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
            "candidate": [{"status": "completed", "metric": 106.2}, {"status": "completed", "metric": 106.2}],
            "correctness": "passed",
            "compile_ok": True,
            "card_seconds": 12.0,
        },
    )
    assert code_ab == 0
    assert res_ab["entry"]["verdict"] == "supported"
    assert (
        res_ab["entry"]["gates"]["gate3_ablation"]["ablation_receipt"]["status"]
        == "verified_from_orthogonal_parents"
    )

    # Verify cumulative speedup honesty: must be 1.062x, NEVER 1.03 * 1.032 * 1.062!
    live, _ = ledger.fold(ledger.iter_ledger(ledger_path, task_id="qwen3_decode_opt"))
    ranked = ledger._rank_supported_candidates(live)
    best_rec, step_ratio, cum_ratio = ranked[0]
    assert best_rec["exp_id"] == "exp_cross_ok"
    assert step_ratio == 1.062
    assert cum_ratio == 1.062
    # Already-recorded pair should no longer be re-suggested
    assert suggest_orthogonal_crossovers(live) == []

    # Idempotent retry of `record` with bottleneck_lane / parent_exp_ids must succeed without conflict
    res_retry, code_retry = rsi_loop.complete(
        state_path,
        "record",
        {
            "bottleneck_summary": {"top_block": "Attention+Norm"},
            "profile_ref": "prof_1.json",
            "unit_refs": [],
            "skipped_reason": "orthogonal crossover",
            "exp_id": "exp_cross_ok",
            "anchor_baseline_exp_id": "initial",
            "parent_exp_ids": ["exp_attn", "exp_norm"],
            "hypothesis": "combine orthogonal FlashAttention prefetch and fused RMSNorm",
            "candidate_repo": str(cand_ab),
            "factors": ["attn_prefetch", "rmsnorm_residual_fuse"],
            "run_ids": ["run_ab1"],
            "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
            "candidate": [{"status": "completed", "metric": 106.2}, {"status": "completed", "metric": 106.2}],
            "correctness": "passed",
            "compile_ok": True,
            "card_seconds": 12.0,
        },
    )
    assert code_retry == 0
    assert res_retry.get("recovered_append") is True


def test_crossover_edge_invariants_three_parents_anchor_and_relative_margin(tmp_path: Path) -> None:
    from mindie_knowledge.graded.rsi_crossover import (
        derive_crossover_ablation_receipt,
        normalize_parent_exp_ids,
    )

    # 1. Shared non-initial anchor Bk must NOT be injected into constituent parent_exp_ids ["a", "b"]
    assert normalize_parent_exp_ids(["a", "b"], exp_id="ab", baseline_exp_id="bk") == ["a", "b"]
    assert normalize_parent_exp_ids(["b"], exp_id="ab", baseline_exp_id="a") == ["a", "b"]

    # 2. Multiplicative noise margin: 2.015x over 2.0x parent is +0.75% (< 1% margin) -> pruned!
    live_two = [
        {
            "exp_id": "a_old",
            "_superseded_ids": ["a_v0"],
            "verdict": "supported",
            "speedup_ratio": 2.0,
            "source_digest": "src_a",
            "baseline_source_digest": "src_bk",
            "baseline_exp_id": "bk",
            "conditions": {"bs": 32},
        },
        {
            "exp_id": "b",
            "verdict": "subtarget_valid",
            "speedup_ratio": 1.03,
            "source_digest": "src_b",
            "baseline_source_digest": "src_bk",
            "baseline_exp_id": "bk",
            "conditions": {"bs": 32},
        },
    ]
    # Also verifies superseded parent alias "a_v0" resolves cleanly!
    rec_small = derive_crossover_ablation_receipt(
        live_two,
        parent_exp_ids=["a_v0", "b"],
        baseline_source_digest="src_bk",
        conditions={"bs": 32},
        combined_speedup_ratio=2.015,
        noise_margin=0.01,
    )
    assert rec_small is not None and rec_small["ablation_retained"] is False

    # 3. Three-parent crossover A+B+C MUST NOT pass LOO from single parents A, B, C alone!
    live_three_single_only = live_two + [
        {
            "exp_id": "c",
            "verdict": "subtarget_valid",
            "speedup_ratio": 1.025,
            "source_digest": "src_c",
            "baseline_source_digest": "src_bk",
            "baseline_exp_id": "bk",
            "conditions": {"bs": 32},
        }
    ]
    assert (
        derive_crossover_ablation_receipt(
            live_three_single_only,
            parent_exp_ids=["a_old", "b", "c"],
            baseline_source_digest="src_bk",
            conditions={"bs": 32},
            combined_speedup_ratio=2.20,
            noise_margin=0.01,
        )
        is None
    )

    # Once all three (N-1) pairwise controls (A+B, A+C, B+C) are verified, 3-way LOO succeeds:
    live_three_with_pairs = live_three_single_only + [
        {
            "exp_id": "ab",
            "parent_exp_ids": ["a_old", "b"],
            "verdict": "supported",
            "speedup_ratio": 2.08,
            "source_digest": "src_ab",
            "baseline_source_digest": "src_bk",
            "conditions": {"bs": 32},
        },
        {
            "exp_id": "ac",
            "parent_exp_ids": ["a_old", "c"],
            "verdict": "supported",
            "speedup_ratio": 2.06,
            "source_digest": "src_ac",
            "baseline_source_digest": "src_bk",
            "conditions": {"bs": 32},
        },
        {
            "exp_id": "bc",
            "parent_exp_ids": ["b", "c"],
            "verdict": "supported",
            "speedup_ratio": 1.06,
            "source_digest": "src_bc",
            "baseline_source_digest": "src_bk",
            "conditions": {"bs": 32},
        },
    ]
    rec_three_ok = derive_crossover_ablation_receipt(
        live_three_with_pairs,
        parent_exp_ids=["a_old", "b", "c"],
        baseline_source_digest="src_bk",
        conditions={"bs": 32},
        combined_speedup_ratio=2.15,
        noise_margin=0.01,
    )
    assert rec_three_ok is not None and rec_three_ok["ablation_retained"] is True

    # 4. Propose with anchor_baseline_exp_id exposes the selected anchor in measure step output
    base_repo = _init_git_repo(tmp_path / "loop_anchor_base", "v0\n")
    cand1 = _init_git_repo(tmp_path / "loop_anchor_c1", "v1\n")
    cand2 = _init_git_repo(tmp_path / "loop_anchor_c2", "v2\n")
    st_path = tmp_path / "st.json"
    lg_path = tmp_path / "lg.jsonl"
    init_state = rsi_loop.init(
        st_path,
        task_id="anchor_task",
        ledger_path=lg_path,
        baseline_repo=base_repo,
        direction="maximize",
        repetitions=2,
        target_speedup=1.05,
    )
    initial_digest = init_state["baseline_source_digest"]
    rsi_loop.complete(
        st_path,
        "record",
        {
            "bottleneck_summary": {"top": "k"},
            "profile_ref": "p0.json",
            "unit_refs": [],
            "skipped_reason": "s",
            "exp_id": "e1",
            "hypothesis": "h1",
            "candidate_repo": str(cand1),
            "run_ids": ["r1"],
            "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
            "candidate": [{"status": "completed", "metric": 110.0}, {"status": "completed", "metric": 110.0}],
            "correctness": "passed",
            "compile_ok": True,
        },
    )
    rsi_loop.complete(st_path, "profile", {"bottleneck_summary": {"top": "k"}, "profile_ref": "p1.json"})
    rsi_loop.complete(st_path, "consult", {"mode": "reuse", "unit_refs": [], "skipped_reason": "s"})
    prop_out, prop_code = rsi_loop.complete(
        st_path,
        "propose",
        {
            "exp_id": "e2",
            "hypothesis": "h2",
            "anchor_baseline_exp_id": "initial",
            "candidate_repo": str(cand2),
        },
    )
    assert prop_code == 0
    assert prop_out["step"] == "measure"
    assert prop_out["baseline_source_digest"] == initial_digest
    assert rsi_loop.next_step(st_path)["baseline_source_digest"] == initial_digest


def test_crossover_factor_coverage_pending_guard_strongest_control_and_retry_stability(
    tmp_path: Path,
) -> None:
    from mindie_knowledge.graded.rsi_crossover import derive_crossover_ablation_receipt

    # [P1-1] Candidate adding unmeasured factor "f_c" beyond parents ["f_a"], ["f_b"] must NOT auto-pass
    live_ab = [
        {
            "exp_id": "a",
            "verdict": "subtarget_valid",
            "speedup_ratio": 1.03,
            "factors": ["f_a"],
            "source_digest": "src_a",
            "baseline_source_digest": "src_bk",
            "conditions": {"bs": 16},
        },
        {
            "exp_id": "b",
            "verdict": "subtarget_valid",
            "speedup_ratio": 1.04,
            "factors": ["f_b"],
            "source_digest": "src_b",
            "baseline_source_digest": "src_bk",
            "conditions": {"bs": 16},
        },
    ]
    assert (
        derive_crossover_ablation_receipt(
            live_ab,
            parent_exp_ids=["a", "b"],
            baseline_source_digest="src_bk",
            conditions={"bs": 16},
            combined_speedup_ratio=1.10,
            candidate_factors=["f_a", "f_b", "f_c"],
        )
        is None
    )

    # [P1-2] Multi-parent candidate without `factors` and without valid parent controls stays ablation_pending
    unverified_entry = ledger.judge(
        {
            "exp_id": "ab_no_factors",
            "hypothesis": "combine missing parents",
            "parent_exp_ids": ["missing_a", "missing_b"],
            "run_ids": ["r_unv"],
            "run_status": "completed",
            "source_digest": "s_cand",
            "baseline_source_digest": "s_base",
            "repetitions": 2,
            "direction": "maximize",
            "target_speedup": 1.05,
            "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
            "candidate": [{"status": "completed", "metric": 112.0}, {"status": "completed", "metric": 112.0}],
            "correctness": "passed",
            "compile_ok": True,
        }
    )
    assert unverified_entry["verdict"] == "inconclusive"
    assert unverified_entry["outcome"] == "ablation_pending"

    # [P2-2] Active non-kept live ID "a" must NOT resolve via superseded alias on kept "a_v2"
    live_colliding_alias = [
        {
            "exp_id": "a_v2",
            "_superseded_ids": ["a"],
            "verdict": "supported",
            "speedup_ratio": 1.08,
            "source_digest": "src_av2",
            "baseline_source_digest": "src_bk",
            "conditions": {"bs": 16},
        },
        {
            "exp_id": "a",
            "verdict": "refuted",
            "speedup_ratio": 0.92,
            "source_digest": "src_a_bad",
            "baseline_source_digest": "src_bk",
            "conditions": {"bs": 16},
        },
        {
            "exp_id": "b",
            "verdict": "supported",
            "speedup_ratio": 1.06,
            "source_digest": "src_b",
            "baseline_source_digest": "src_bk",
            "conditions": {"bs": 16},
        },
    ]
    assert (
        derive_crossover_ablation_receipt(
            live_colliding_alias,
            parent_exp_ids=["a", "b"],
            baseline_source_digest="src_bk",
            conditions={"bs": 16},
            combined_speedup_ratio=1.18,
        )
        is None
    )

    # [P2-1] Strongest duplicate LOO control (1.20x) wins even if weaker duplicate (1.10x) appears later,
    # AND [P2-4] BC measured against AB resolves independent of parent ID sort order ("a","b","c")
    live_topo_and_dup = [
        {"exp_id": "a", "verdict": "supported", "speedup_ratio": 1.05, "source_digest": "d_a", "baseline_source_digest": "d_bk", "conditions": {"bs": 8}},
        {"exp_id": "b", "verdict": "supported", "speedup_ratio": 1.06, "source_digest": "d_b", "baseline_source_digest": "d_bk", "conditions": {"bs": 8}},
        {"exp_id": "c", "verdict": "supported", "speedup_ratio": 1.07, "source_digest": "d_c", "baseline_source_digest": "d_bk", "conditions": {"bs": 8}},
        {"exp_id": "ab_strong", "parent_exp_ids": ["a", "b"], "verdict": "supported", "speedup_ratio": 1.20, "source_digest": "d_ab1", "baseline_source_digest": "d_bk", "conditions": {"bs": 8}},
        {"exp_id": "ab_weak", "parent_exp_ids": ["a", "b"], "verdict": "supported", "speedup_ratio": 1.10, "source_digest": "d_ab2", "baseline_source_digest": "d_bk", "conditions": {"bs": 8}},
        {"exp_id": "ac", "parent_exp_ids": ["a", "c"], "verdict": "supported", "speedup_ratio": 1.11, "source_digest": "d_ac", "baseline_source_digest": "d_bk", "conditions": {"bs": 8}},
        # BC measured incrementally against ab_weak (1.10 * 1.02 = 1.122x vs d_bk), evaluated BEFORE "ab_*" in sorted omit order!
        {"exp_id": "bc", "parent_exp_ids": ["b", "c"], "verdict": "supported", "speedup_ratio": 1.02, "source_digest": "d_bc", "baseline_source_digest": "d_ab2", "baseline_exp_id": "ab_weak", "conditions": {"bs": 8}},
    ]
    # 1.15x < ab_strong (1.20x) -> must be pruned (never allowed to pass via later ab_weak 1.10x!)
    rec_dup_pruned = derive_crossover_ablation_receipt(
        live_topo_and_dup,
        parent_exp_ids=["a", "b", "c"],
        baseline_source_digest="d_bk",
        conditions={"bs": 8},
        combined_speedup_ratio=1.15,
        noise_margin=0.01,
    )
    assert rec_dup_pruned is not None and rec_dup_pruned["ablation_retained"] is False
    assert rec_dup_pruned["dominant_single_parent"] == "ab_strong"
    assert rec_dup_pruned["parent_speedups_vs_bk"]["bc"] == 1.122

    # 1.25x > 1.20x * 1.01 -> passes cleanly with BC resolved topologically!
    rec_topo_ok = derive_crossover_ablation_receipt(
        live_topo_and_dup,
        parent_exp_ids=["a", "b", "c"],
        baseline_source_digest="d_bk",
        conditions={"bs": 8},
        combined_speedup_ratio=1.25,
        noise_margin=0.01,
    )
    assert rec_topo_ok is not None and rec_topo_ok["ablation_retained"] is True

    # [P2-3] Delayed-distill / idempotent `record` retry succeeds even after a parent is superseded under a new ID
    r_base = _init_git_repo(tmp_path / "retry_base", "b0\n")
    r_pa = _init_git_repo(tmp_path / "retry_pa", "pa\n")
    r_pa2 = _init_git_repo(tmp_path / "retry_pa2", "pa2\n")
    r_pb = _init_git_repo(tmp_path / "retry_pb", "pb\n")
    r_ab = _init_git_repo(tmp_path / "retry_ab", "pab\n")
    st = tmp_path / "retry_loop.json"
    lg = tmp_path / "retry_ledger.jsonl"
    rsi_loop.init(st, task_id="retry_task", ledger_path=lg, baseline_repo=r_base, direction="maximize", repetitions=2, target_speedup=1.05)

    for eid, repo, fac, metric in (("pa", r_pa, "fa", 103.0), ("pb", r_pb, "fb", 103.5)):
        _, rc = rsi_loop.complete(
            st,
            "record",
            {
                "bottleneck_summary": {"top": fac},
                "profile_ref": "p.json",
                "unit_refs": [],
                "skipped_reason": "s",
                "exp_id": eid,
                "anchor_baseline_exp_id": "initial",
                "hypothesis": f"opt {eid}",
                "candidate_repo": str(repo),
                "factors": [fac],
                "run_ids": [f"run_{eid}"],
                "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
                "candidate": [{"status": "completed", "metric": metric}, {"status": "completed", "metric": metric}],
                "correctness": "passed",
                "compile_ok": True,
            },
        )
        assert rc == 0

    ab_payload = {
        "bottleneck_summary": {"top": "fa+fb"},
        "profile_ref": "p.json",
        "unit_refs": [],
        "skipped_reason": "s",
        "exp_id": "pab",
        "anchor_baseline_exp_id": "initial",
        "parent_exp_ids": ["pa", "pb"],
        "hypothesis": "combine pa and pb",
        "candidate_repo": str(r_ab),
        "factors": ["fa", "fb"],
        "run_ids": ["run_pab"],
        "baseline": [{"status": "completed", "metric": 100.0}, {"status": "completed", "metric": 100.0}],
        "candidate": [{"status": "completed", "metric": 108.0}, {"status": "completed", "metric": 108.0}],
        "correctness": "passed",
        "compile_ok": True,
    }
    res_first, rc_first = rsi_loop.complete(st, "record", ab_payload)
    assert rc_first == 0 and res_first["entry"]["verdict"] == "supported"

    # Supersede parent "pa" -> "pa_v2" directly in ledger
    live_now, _ = ledger.fold(ledger.iter_ledger(lg, task_id="retry_task"))
    pa_rec = next(r for r in live_now if r["exp_id"] == "pa")
    pa_v2 = dict(pa_rec, exp_id="pa_v2", candidate_id="pa_v2", supersedes_exp_id="pa", run_ids=["run_pa_v2"], source_digest=ledger.fingerprint(r_pa2)["source_digest"])
    ledger.append(lg, pa_v2, progress=st.parent / "PROGRESS.md")

    # Replay pab with delayed distillation receipt attached: must preserve committed ablation receipt & succeed!
    res_delayed, rc_delayed = rsi_loop.complete(
        st,
        "record",
        {**ab_payload, "distill": {"unit_id": "ku_pab_1", "status": "distilled"}},
    )
    assert rc_delayed == 0
    assert res_delayed.get("recovered_append") is True


def test_alias_lineage_and_factor_prefilter_regressions(tmp_path: Path) -> None:
    from mindie_knowledge.graded.rsi_crossover import derive_crossover_ablation_receipt

    # 1. Non-kept superseded baseline alias (`bk` -> `bk_bad`, verdict="refuted") invalidates child crossovers
    lg_bad = tmp_path / "bad_bk.jsonl"
    for row in (
        {"task_id": "t1", "exp_id": "bk", "source_digest": "d_bk", "baseline_source_digest": "d_0", "verdict": "supported", "speedup_ratio": 1.2, "conditions": {}, "run_ids": ["r0"]},
        {"task_id": "t1", "exp_id": "a", "source_digest": "d_a", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "factors": ["fa"], "verdict": "supported", "speedup_ratio": 1.05, "conditions": {}, "run_ids": ["ra"]},
        {"task_id": "t1", "exp_id": "b", "source_digest": "d_b", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "factors": ["fb"], "verdict": "supported", "speedup_ratio": 1.06, "conditions": {}, "run_ids": ["rb"]},
        {"task_id": "t1", "exp_id": "bk_bad", "supersedes_exp_id": "bk", "source_digest": "d_bk_bad", "baseline_source_digest": "d_0", "verdict": "refuted", "speedup_ratio": 0.9, "conditions": {}, "run_ids": ["r_bad"]},
    ):
        ledger.append(lg_bad, row, progress=tmp_path / "P_bad.md")
    live_bad, _ = ledger.fold(ledger.iter_ledger(lg_bad, task_id="t1"))
    assert suggest_orthogonal_crossovers(live_bad) == []
    assert (
        derive_crossover_ablation_receipt(
            live_bad,
            parent_exp_ids=["a", "b"],
            baseline_source_digest="d_bk",
            baseline_exp_id="bk",
            conditions={},
            combined_speedup_ratio=1.15,
            candidate_factors=["fa", "fb"],
        )
        is None
    )

    # 2 & 3. Kept superseded baseline alias (`bk` -> `bk_v2`) + parents without `factors` metadata
    lg_kept = tmp_path / "kept_bk.jsonl"
    for row in (
        {"task_id": "t2", "exp_id": "bk", "source_digest": "d_bk", "baseline_source_digest": "d_0", "verdict": "supported", "speedup_ratio": 1.2, "conditions": {}, "run_ids": ["k0"], "candidate_repo": "/tmp/r_bk"},
        {"task_id": "t2", "exp_id": "a", "source_digest": "d_a", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "bottleneck_lane": "Attention", "verdict": "supported", "speedup_ratio": 1.05, "conditions": {}, "run_ids": ["ka"]},
        {"task_id": "t2", "exp_id": "b", "source_digest": "d_b", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "bottleneck_lane": "Norm", "verdict": "supported", "speedup_ratio": 1.06, "conditions": {}, "run_ids": ["kb"]},
        {"task_id": "t2", "exp_id": "bk_v2", "supersedes_exp_id": "bk", "source_digest": "d_bk", "baseline_source_digest": "d_0", "verdict": "supported", "speedup_ratio": 1.21, "conditions": {}, "run_ids": ["k0_v2"], "candidate_repo": "/tmp/r_bk"},
    ):
        ledger.append(lg_kept, row, progress=tmp_path / "P_kept.md")
    live_kept, _ = ledger.fold(ledger.iter_ledger(lg_kept, task_id="t2"))
    hints = suggest_orthogonal_crossovers(live_kept)
    assert len(hints) == 1
    assert hints[0]["shared_baseline_exp_id"] == "bk_v2"
    assert hints[0]["suggested_factors"] is None

    dummy_state = {"ledger": str(lg_kept), "task_id": "t2", "baseline": {"repo": "/tmp/r0", "source_digest": "d_b", "exp_id": "b"}}
    assert rsi_loop._resolve_anchor_baseline(dummy_state, "bk")["exp_id"] == "bk_v2"
    assert rsi_loop._resolve_anchor_baseline(dummy_state, "bk_v2")["exp_id"] == "bk_v2"
    rec_no_fac = derive_crossover_ablation_receipt(
        live_kept,
        parent_exp_ids=["a", "b"],
        baseline_source_digest="d_bk",
        baseline_exp_id="bk_v2",
        conditions={},
        combined_speedup_ratio=1.12,
        candidate_factors=hints[0]["suggested_factors"],
    )
    assert rec_no_fac is not None and rec_no_fac["ablation_retained"] is True

    # 4. Pre-filter LOO factor sets before choosing strongest (N-1) control (ignores faster AB+X when evaluating A+B+C)
    live_abc = [
        {"exp_id": "a", "verdict": "supported", "speedup_ratio": 1.08, "source_digest": "sa", "baseline_source_digest": "s0", "factors": ["fa"], "conditions": {}},
        {"exp_id": "b", "verdict": "supported", "speedup_ratio": 1.09, "source_digest": "sb", "baseline_source_digest": "s0", "factors": ["fb"], "conditions": {}},
        {"exp_id": "c", "verdict": "supported", "speedup_ratio": 1.07, "source_digest": "sc", "baseline_source_digest": "s0", "factors": ["fc"], "conditions": {}},
        {"exp_id": "ab", "verdict": "supported", "speedup_ratio": 1.18, "source_digest": "sab", "baseline_source_digest": "s0", "parent_exp_ids": ["a", "b"], "factors": ["fa", "fb"], "conditions": {}},
        {"exp_id": "ab_x", "verdict": "supported", "speedup_ratio": 1.45, "source_digest": "sabx", "baseline_source_digest": "s0", "parent_exp_ids": ["a", "b"], "factors": ["fa", "fb", "fx"], "conditions": {}},
        {"exp_id": "ac", "verdict": "supported", "speedup_ratio": 1.16, "source_digest": "sac", "baseline_source_digest": "s0", "parent_exp_ids": ["a", "c"], "factors": ["fa", "fc"], "conditions": {}},
        {"exp_id": "bc", "verdict": "supported", "speedup_ratio": 1.17, "source_digest": "sbc", "baseline_source_digest": "s0", "parent_exp_ids": ["b", "c"], "factors": ["fb", "fc"], "conditions": {}},
    ]
    rec_abc = derive_crossover_ablation_receipt(
        live_abc,
        parent_exp_ids=["a", "b", "c"],
        baseline_source_digest="s0",
        conditions={},
        combined_speedup_ratio=1.28,
        candidate_factors=["fa", "fb", "fc"],
    )
    assert rec_abc is not None and rec_abc["ablation_retained"] is True
    assert set(rec_abc["parent_speedups_vs_bk"].keys()) == {"ab", "ac", "bc"}


def test_source_bound_controls_global_id_reservation_incremental_3way_and_digest_only_anchor(
    tmp_path: Path,
) -> None:
    from mindie_knowledge.graded.rsi_crossover import derive_crossover_ablation_receipt

    # 1. [P1] Source-changing supersession (both new exp_id a->a2 and in-place a->a) must NOT reuse stale AB/AC controls
    for mode in ("new_id", "in_place"):
        lg_p1 = tmp_path / f"p1_{mode}.jsonl"
        rows = [
            {"task_id": "tp1", "exp_id": "bk", "source_digest": "d_bk", "baseline_source_digest": "d_0", "verdict": "supported", "speedup_ratio": 1.1, "conditions": {"bs": 16}, "run_ids": ["r0"]},
            {"task_id": "tp1", "exp_id": "a", "source_digest": "d_a1", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "factors": ["fa"], "verdict": "supported", "speedup_ratio": 1.05, "conditions": {"bs": 16}, "run_ids": ["ra1"]},
            {"task_id": "tp1", "exp_id": "b", "source_digest": "d_b", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "factors": ["fb"], "verdict": "supported", "speedup_ratio": 1.05, "conditions": {"bs": 16}, "run_ids": ["rb"]},
            {"task_id": "tp1", "exp_id": "c", "source_digest": "d_c", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "factors": ["fc"], "verdict": "supported", "speedup_ratio": 1.05, "conditions": {"bs": 16}, "run_ids": ["rc"]},
            {"task_id": "tp1", "exp_id": "ab", "source_digest": "d_ab", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "parent_exp_ids": ["a", "b"], "factors": ["fa", "fb"], "verdict": "supported", "speedup_ratio": 1.12, "conditions": {"bs": 16}, "run_ids": ["rab"]},
            {"task_id": "tp1", "exp_id": "ac", "source_digest": "d_ac", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "parent_exp_ids": ["a", "c"], "factors": ["fa", "fc"], "verdict": "supported", "speedup_ratio": 1.12, "conditions": {"bs": 16}, "run_ids": ["rac"]},
            {"task_id": "tp1", "exp_id": "bc", "source_digest": "d_bc", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "parent_exp_ids": ["b", "c"], "factors": ["fb", "fc"], "verdict": "supported", "speedup_ratio": 1.12, "conditions": {"bs": 16}, "run_ids": ["rbc"]},
            {
                "task_id": "tp1",
                "exp_id": "a2" if mode == "new_id" else "a",
                "supersedes_exp_id": "a",
                "source_digest": "d_a2",
                "baseline_source_digest": "d_bk",
                "baseline_exp_id": "bk",
                "factors": ["fa"],
                "verdict": "supported",
                "speedup_ratio": 1.07,
                "conditions": {"bs": 16},
                "run_ids": ["ra2"],
            },
        ]
        for row in rows:
            ledger.append(lg_p1, row, progress=tmp_path / f"P_{mode}.md")
        folded_p1, _ = ledger.fold(ledger.iter_ledger(lg_p1, task_id="tp1"))
        target_a = "a2" if mode == "new_id" else "a"
        assert (
            derive_crossover_ablation_receipt(
                folded_p1,
                parent_exp_ids=[target_a, "b", "c"],
                baseline_source_digest="d_bk",
                baseline_exp_id="bk",
                conditions={"bs": 16},
                combined_speedup_ratio=1.25,
                candidate_factors=["fa", "fb", "fc"],
            )
            is None
        )

    # 2. [P2-1] Task-global primary ID reservation across condition scopes
    lg_global = tmp_path / "global_id.jsonl"
    for row in (
        {"task_id": "tg", "exp_id": "a", "source_digest": "d_a", "baseline_source_digest": "d_bk", "verdict": "supported", "speedup_ratio": 1.05, "conditions": {"bs": 16}, "run_ids": ["g1"]},
        {"task_id": "tg", "exp_id": "b", "source_digest": "d_b", "baseline_source_digest": "d_bk", "verdict": "supported", "speedup_ratio": 1.06, "conditions": {"bs": 16}, "run_ids": ["g2"]},
        {"task_id": "tg", "exp_id": "a_v2", "supersedes_exp_id": "a", "source_digest": "d_a", "baseline_source_digest": "d_bk", "verdict": "supported", "speedup_ratio": 1.05, "conditions": {"bs": 16}, "run_ids": ["g3"]},
        {"task_id": "tg", "exp_id": "a", "source_digest": "d_a_bs32", "baseline_source_digest": "d_bk32", "verdict": "refuted", "speedup_ratio": 0.88, "conditions": {"bs": 32}, "run_ids": ["g4"]},
    ):
        ledger.append(lg_global, row, progress=tmp_path / "P_global.md")
    folded_global, _ = ledger.fold(ledger.iter_ledger(lg_global, task_id="tg"))
    assert (
        derive_crossover_ablation_receipt(
            folded_global,
            parent_exp_ids=["a", "b"],
            baseline_source_digest="d_bk",
            conditions={"bs": 16},
            combined_speedup_ratio=1.15,
        )
        is None
    )

    # 3. [P2-2] 3-parent crossover measured incrementally against 1-parent baseline "a"
    live_3way = [
        {"exp_id": "a", "verdict": "supported", "speedup_ratio": 1.08, "source_digest": "sa", "baseline_source_digest": "s0", "factors": ["fa"], "conditions": {}},
        {"exp_id": "b", "verdict": "supported", "speedup_ratio": 1.09, "source_digest": "sb", "baseline_source_digest": "s0", "factors": ["fb"], "conditions": {}},
        {"exp_id": "c", "verdict": "supported", "speedup_ratio": 1.07, "source_digest": "sc", "baseline_source_digest": "s0", "factors": ["fc"], "conditions": {}},
        {"exp_id": "ab", "verdict": "supported", "speedup_ratio": 1.18, "source_digest": "sab", "baseline_source_digest": "s0", "parent_exp_ids": ["a", "b"], "factors": ["fa", "fb"], "conditions": {}},
        {"exp_id": "ac", "verdict": "supported", "speedup_ratio": 1.16, "source_digest": "sac", "baseline_source_digest": "s0", "parent_exp_ids": ["a", "c"], "factors": ["fa", "fc"], "conditions": {}},
        {"exp_id": "bc", "verdict": "supported", "speedup_ratio": 1.17, "source_digest": "sbc", "baseline_source_digest": "s0", "parent_exp_ids": ["b", "c"], "factors": ["fb", "fc"], "conditions": {}},
    ]
    rec_inc_3way = derive_crossover_ablation_receipt(
        live_3way, parent_exp_ids=["a", "b", "c"], baseline_source_digest="sa", baseline_exp_id="a",
        conditions={}, combined_speedup_ratio=1.20, candidate_factors=["fa", "fb", "fc"],
    )
    assert rec_inc_3way is not None and rec_inc_3way["ablation_retained"] is True and rec_inc_3way["crossover_speedup_vs_bk"] == 1.296

    # 4. [P2-3] Canonicalize unambiguous digest-only sibling baseline references
    sug = suggest_orthogonal_crossovers([
        {"exp_id": "bk", "verdict": "supported", "speedup_ratio": 1.10, "source_digest": "d_bk", "baseline_source_digest": "d_0", "conditions": {}},
        {"exp_id": "s1", "verdict": "supported", "speedup_ratio": 1.05, "source_digest": "d_s1", "baseline_source_digest": "d_bk", "baseline_exp_id": "bk", "bottleneck_lane": "Attention", "conditions": {}},
        {"exp_id": "s2", "verdict": "supported", "speedup_ratio": 1.06, "source_digest": "d_s2", "baseline_source_digest": "d_bk", "bottleneck_lane": "MoE", "conditions": {}},
    ])
    assert len(sug) == 1 and sug[0]["parent_exp_ids"] == ["s1", "s2"] and sug[0]["shared_baseline_exp_id"] == "bk"

    # 5. Proposal-pinned parent source digests, refold alias preservation, and live primary precedence
    assert derive_crossover_ablation_receipt(
        [{"exp_id": "a", "verdict": "supported", "speedup_ratio": 1.05, "source_digest": "sa_v2", "baseline_source_digest": "s0", "conditions": {}},
         {"exp_id": "b", "verdict": "supported", "speedup_ratio": 1.10, "source_digest": "sb_v1", "baseline_source_digest": "s0", "conditions": {}}],
        parent_exp_ids=["a", "b"], baseline_source_digest="s0", conditions={}, combined_speedup_ratio=1.16,
        parent_source_digests={"a": "sa_v1", "b": "sb_v1"},
    ) is None
    lp = tmp_path / "refold_ledger.jsonl"
    pp = tmp_path / "REFOLD_PROGRESS.md"
    for row in (
        {"task_id": "rf", "exp_id": "a", "verdict": "supported", "speedup_ratio": 1.20, "source_digest": "sa1", "baseline_source_digest": "s0", "run_ids": ["r1"], "conditions": {}},
        {"task_id": "rf", "exp_id": "b", "verdict": "supported", "speedup_ratio": 1.10, "source_digest": "sb1", "baseline_source_digest": "sa1", "baseline_exp_id": "a", "run_ids": ["r2"], "conditions": {}},
        {"task_id": "rf", "exp_id": "a2", "supersedes_exp_id": "a", "verdict": "supported", "speedup_ratio": 1.20, "source_digest": "sa1", "baseline_source_digest": "s0", "run_ids": ["r1"], "conditions": {}},
    ):
        ledger.append(lp, dict(row), progress=pp)
    assert "candidate: `b`" in pp.read_text(encoding="utf-8") and "1.3200x" in pp.read_text(encoding="utf-8")
    live_reuse, _ = ledger.fold([
        {"task_id": "ru", "exp_id": "a", "verdict": "supported", "speedup_ratio": 1.05, "source_digest": "sa_old", "baseline_source_digest": "s0", "run_ids": ["r1"], "conditions": {}},
        {"task_id": "ru", "exp_id": "r_bad", "supersedes_exp_id": "a", "verdict": "refuted", "speedup_ratio": 0.90, "source_digest": "s_bad", "baseline_source_digest": "s0", "run_ids": ["r2"], "conditions": {}},
        {"task_id": "ru", "exp_id": "a", "verdict": "supported", "speedup_ratio": 1.06, "source_digest": "sa_new", "baseline_source_digest": "s0", "run_ids": ["r3"], "conditions": {}},
        {"task_id": "ru", "exp_id": "b", "verdict": "supported", "speedup_ratio": 1.04, "source_digest": "sb_new", "baseline_source_digest": "sa_new", "baseline_exp_id": "a", "run_ids": ["r4"], "conditions": {}},
    ])
    assert derive_crossover_ablation_receipt(
        [*live_reuse, {"exp_id": "c", "verdict": "supported", "speedup_ratio": 1.04, "source_digest": "sc_new", "baseline_source_digest": "sa_new", "baseline_exp_id": "a", "conditions": {}}],
        parent_exp_ids=["b", "c"], baseline_source_digest="sa_new", conditions={}, combined_speedup_ratio=1.09,
    )["ablation_retained"] is True
    r0, ra1, ra2, rb1, rab, rabr = (_init_git_repo(tmp_path / n, f"{n}\n") for n in ("p0", "pa1", "pa2", "pb1", "pab", "pabr"))
    st2, lg2 = tmp_path / "pin_loop" / "rsi-loop.json", tmp_path / "pin_loop" / "experiment-ledger.jsonl"
    rsi_loop.init(st2, task_id="pin", ledger_path=lg2, baseline_repo=r0, repetitions=2, direction="maximize")
    for eid, rp, met in (("a", ra1, 120.0), ("b", rb1, 110.0)):
        rsi_loop.complete(st2, "record", {"bottleneck_summary": {"k": 1}, "profile_ref": "p", "unit_refs": [], "skipped_reason": "s", "exp_id": eid, "anchor_baseline_exp_id": "initial", "hypothesis": eid, "candidate_repo": str(rp), "run_ids": [f"r_{eid}"], "baseline": [{"status": "completed", "metric": 100.0}] * 2, "candidate": [{"status": "completed", "metric": met}] * 2, "correctness": "passed", "compile_ok": True})
    retry_body = {"bottleneck_summary": {"k": 1}, "profile_ref": "p", "unit_refs": [], "skipped_reason": "s", "exp_id": "ab_r", "anchor_baseline_exp_id": "initial", "parent_exp_ids": ["a", "b"], "parent_source_digests": {}, "hypothesis": "ab_r", "candidate_repo": str(rabr), "run_ids": ["r_abr"], "baseline": [{"status": "completed", "metric": 100.0}] * 2, "candidate": [{"status": "completed", "metric": 135.0}] * 2, "correctness": "passed", "compile_ok": True}
    assert rsi_loop.complete(st2, "record", dict(retry_body))[0]["entry"]["verdict"] == "supported"
    rsi_loop.complete(st2, "profile", {"bottleneck_summary": {"k": 1}, "profile_ref": "p"})
    rsi_loop.complete(st2, "consult", {"mode": "reuse", "unit_refs": [], "skipped_reason": "s"})
    rsi_loop.complete(st2, "propose", {"exp_id": "ab", "anchor_baseline_exp_id": "initial", "parent_exp_ids": ["a", "b"], "hypothesis": "ab", "candidate_repo": str(rab)})
    a_now = next(r for r in ledger.fold(ledger.iter_ledger(lg2, task_id="pin"))[0] if r["exp_id"] == "a")
    ledger.append(lg2, dict(a_now, supersedes_exp_id="a", speedup_ratio=1.05, source_digest=ledger.fingerprint(ra2)["source_digest"], run_ids=["r_a2"]), progress=st2.parent / "PROGRESS.md")
    m_res, rc_m = rsi_loop.complete(st2, "measure", {"run_ids": ["r_ab"], "baseline": [{"status": "completed", "metric": 100.0}] * 2, "candidate": [{"status": "completed", "metric": 116.0}] * 2, "correctness": "passed", "compile_ok": True})
    assert rc_m == 0 and m_res["entry"]["verdict"] == "inconclusive" and m_res["entry"]["outcome"] == "ablation_pending"
    rsi_loop.complete(st2, "learn", {"note": "ablation pending after parent supersession"})
    assert rsi_loop.complete(st2, "record", dict(retry_body, note="delayed note"))[0]["entry"]["verdict"] == "supported"
