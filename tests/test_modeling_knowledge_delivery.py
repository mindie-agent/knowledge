"""[Role] Verify outward GradedRepository delivery, budget bounds, and domain overlays for modeling units.
[Invariants] Every delivered reuse/explain payload fits within <= 3,000 tokens without truncating inherited L1 axioms, and mismatched or unknown conditions fail closed.
[Timeouts & Locks] Bounded SQLite/index queries inside pytest temporary directories (< 10s); SQLite WAL connections close cleanly on fixture teardown.
[Side-Effect Policy] Never mutates checked-in corpus/ or local production checkouts; all stores and overlays live inside tmp_path_factory.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mindie_knowledge.graded.conditions import match_conditions
from mindie_knowledge.graded.eval_harness import evaluate_retrieval_suite
from mindie_knowledge.graded.feedback import summarize_usage_events
from mindie_knowledge.graded.index import (
    RoutingIndex,
    read_units,
    response_token_estimate,
    token_estimate,
)
from mindie_knowledge.graded.maintenance import build_routing_index, lint_corpus
from mindie_knowledge.graded.repository import GradedRepository
from mindie_knowledge.loop.store import Store

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus"
REAL_ROOT = Path("/Volumes/data/code")
DOMAINS = (
    "knowledge-vllm-ascend",
    "knowledge-operators",
    "knowledge-infer-distributed",
    "knowledge-infer-observability",
)
ALIASES = {
    "knowledge-vllm-ascend": "vllm-ascend",
    "knowledge-operators": "operators",
    "knowledge-infer-distributed": "distributed",
    "knowledge-infer-observability": "observability",
}
DOMAIN_UNITS = {
    "knowledge-vllm-ascend": {
        "unit-vllm-ascend-hbm-memory-hidden-assassins-axioms",
        "unit-vllm-ascend-launch-to-sol-and-counterfactual-sop",
        "unit-vllm-ascend-mlapo-prefetch-ablation-protocol",
        "unit-ascend-day0-vector-fusion-mlapo-prefetch-multistream-mutator",
    },
    "knowledge-operators": {
        "unit-ascend-aclgraph-bucket-padding-and-varlen-axioms",
        "unit-ascend-op-abi-binding-and-single-op-repro-sop",
    },
    "knowledge-infer-distributed": {
        "unit-vllm-ascend-parallel-ep-knee-eplb-and-xpyd-workflow",
    },
    "knowledge-infer-observability": {
        "unit-cann9-steady-state-capture-and-clock-hygiene",
        "unit-mindie-sim-capture-diff-trace-attribution-workflow",
    },
}
L4_ID = "unit-ascend-day0-vector-fusion-mlapo-prefetch-multistream-mutator"
L3_VLLM_ID = "unit-vllm-ascend-mlapo-prefetch-ablation-protocol"
LAUNCH_ID = "unit-vllm-ascend-launch-to-sol-and-counterfactual-sop"
HBM_ID = "unit-vllm-ascend-hbm-memory-hidden-assassins-axioms"
PAD_ID = "unit-ascend-aclgraph-bucket-padding-and-varlen-axioms"
ABI_ID = "unit-ascend-op-abi-binding-and-single-op-repro-sop"
DIFF_ID = "unit-mindie-sim-capture-diff-trace-attribution-workflow"
MEASURE_ID = "unit-cann9-steady-state-capture-and-clock-hygiene"
PARALLEL_ID = "unit-vllm-ascend-parallel-ep-knee-eplb-and-xpyd-workflow"

REVISIONS = {unit.id: unit.revision for unit in read_units(CORPUS)}

OBSERVED = {
    "accelerator": "ascend",
    "soc": "Ascend910B4",
    "cann": "9.0.0",
    "framework": {"vllm-ascend": "0.11.0"},
    "execution_mode": "aclgraph",
    "workload_phase": "decode",
    "model_family": "deepseek-v3",
    "deployment": "pd_disaggregated",
    "parallel_plan": {"dp": 2, "tp": 8, "ep": 16, "pcp": 2, "dcp": 4},
    "measurement_gear": "deep",
    "topology": "a3_superpod",
}
PARALLEL_OBSERVED = {
    **OBSERVED,
    "soc": "Ascend910_93",
    "parallel_plan": {"dp": 2, "tp": 8, "pp": 1, "ep": 16, "pcp": 2, "dcp": 4},
    "runtime_capability": "mrv2_pcp",
    "hbm_ledger_checked": True,
    "prefill_cp_plan": "pcp",
    "decode_cp_plan": "dcp",
}

QUERY_CASES = (
    ("knowledge-vllm-ascend", "launch-to-sol inspect-launch sol feature-deltas mrv2 PCP", LAUNCH_ID, "reuse", OBSERVED, HBM_ID),
    ("knowledge-vllm-ascend", "MLAPO mla_preprocess_only_decode wd_qkv wu_q static weight prefetch", L3_VLLM_ID, "reuse", OBSERVED, HBM_ID),
    ("knowledge-operators", "aclnnFlashAttentionVarLenScore actual_seq_lens padding masked_varlen", PAD_ID, "reuse", OBSERVED, None),
    ("knowledge-operators", "inspect-op-abi repro-op FRACTAL_NZ format 29 named ABI binding", ABI_ID, "reuse", OBSERVED, None),
    ("knowledge-infer-distributed", "EP64 EPLB knee xPyD routing skew num_redundant_experts", PARALLEL_ID, "reuse", PARALLEL_OBSERVED, None),
    ("knowledge-infer-observability", "diff-trace graph lowering host dispatch collective congestion", DIFF_ID, "reuse", OBSERVED, MEASURE_ID),
    ("knowledge-infer-observability", "steady-state capture min-warmup-steps steady-window-steps max-cv-pct", MEASURE_ID, "reuse", OBSERVED, None),
)


def _units(domain: str | None = None):
    return {unit.id: unit for unit in read_units(CORPUS / domain if domain else CORPUS)}


def _body(unit_id: str) -> str:
    return _units()[unit_id].body


def _mindie(domain: str, unit_id: str) -> str:
    return f"mindie://{ALIASES[domain]}/units/{unit_id}@{REVISIONS[unit_id]}"


def _assert_full_body(unit, text: str | None) -> None:
    assert text is not None
    assert text.strip() == unit.body.strip(), unit.id
    assert token_estimate(text) == token_estimate(unit.body), unit.id


def _assert_target_response(response, expected: str, parent: str | None, *, outward: bool = False) -> None:
    assert response["results"], [row["id"] for row in response["results"]]
    assert response["results"][0]["id"] == expected, [row["id"] for row in response["results"]]
    row = response["results"][0]
    assert row["excerpt_truncated"] is False, expected
    assert row["ready_to_apply"] is True, (expected, row.get("inheritance_excluded"))
    assert row["inheritance_excluded"] == [], (expected, row["inheritance_excluded"])
    assert response["excluded_budget"] == [], (expected, response["excluded_budget"])
    if outward:
        assert response["budget_scope"].startswith("whole delivered response")
    if parent:
        inherited = {item["id"]: item for item in row["inherited_rules"]}
        assert parent in inherited, (expected, list(inherited))
        _assert_full_body(_units()[parent], inherited[parent]["content"])


def _copy_overlay(domain: str, root: Path) -> None:
    real = REAL_ROOT / domain
    if not real.is_dir():
        pytest.skip(f"real corpus not present: {real}")
    shutil.copytree(
        real,
        root,
        ignore=shutil.ignore_patterns(
            ".git", ".github", "index", "docs", "tasks", "feedback", "scripts", "__pycache__",
        ),
    )
    for source in (CORPUS / domain).rglob("*"):
        if not source.is_file():
            continue
        if source.suffix != ".md" and source.name != "ablation-receipt-example.json":
            continue
        relative = source.relative_to(CORPUS / domain)
        if "index" in relative.parts:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


@pytest.fixture(scope="module")
def indexes():
    return {domain: RoutingIndex.from_root(CORPUS / domain) for domain in DOMAINS}


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    stores, repositories = {}, {}
    base = tmp_path_factory.mktemp("stores")
    for domain in DOMAINS:
        store = Store(base / domain, ALIASES[domain])
        repositories[domain] = GradedRepository(store)
        repositories[domain].install_root(CORPUS / domain)
        stores[domain] = store
    yield repositories
    for store in stores.values():
        store.close()


@pytest.fixture(scope="module")
def overlays(tmp_path_factory):
    base = tmp_path_factory.mktemp("overlays")
    built = {}
    for domain in DOMAINS:
        root = base / domain
        _copy_overlay(domain, root)
        write = build_routing_index(root)
        assert write["ok"] and write["written"], write
        check = build_routing_index(root, check=True)
        assert check["ok"], check["errors"]
        report = lint_corpus(root)
        assert report["valid"], report["errors"]
        built[domain] = {"root": root, "index": RoutingIndex.from_root(root), "lint": report}
    return built


@pytest.fixture(scope="module")
def overlay_repos(tmp_path_factory, overlays):
    base = tmp_path_factory.mktemp("overlay_stores")
    stores, repositories = {}, {}
    for domain in DOMAINS:
        store = Store(base / domain, ALIASES[domain])
        repositories[domain] = GradedRepository(store)
        repositories[domain].install_root(overlays[domain]["root"])
        stores[domain] = store
    yield repositories
    for store in stores.values():
        store.close()


@pytest.mark.parametrize(("domain", "query", "expected", "mode", "conditions", "parent"), QUERY_CASES)
def test_index_query_delivers_full_closure_within_3000(indexes, domain, query, expected, mode, conditions, parent):
    response = indexes[domain].query(query, conditions=conditions, limit=1, token_budget=3000, mode=mode)
    _assert_target_response(response, expected, parent)
    assert response["usage"]["estimated_tokens"] <= 3000, response["usage"]
    if parent:
        assert response["results"][0]["match_status"] == "matched"


@pytest.mark.parametrize(("domain", "query", "expected", "mode", "conditions", "parent"), QUERY_CASES)
def test_outward_repository_delivery_within_3000(repos, domain, query, expected, mode, conditions, parent):
    response = repos[domain].query(query, conditions=conditions, limit=1, token_budget=3000, mode=mode)
    assert response["budget_met"] is True, (expected, response.get("minimum_tokens"))
    assert response["response_token_estimate"] <= 3000, response["response_token_estimate"]
    _assert_target_response(response, expected, parent, outward=True)


def test_reuse_delivers_complete_hbm_parent_within_2950(repos):
    repo = repos["knowledge-vllm-ascend"]
    query = "MLAPO mla_preprocess_only_decode wd_qkv wu_q static weight prefetch"
    response = repo.query(query, conditions=OBSERVED, limit=1, token_budget=3000)
    assert response["budget_met"] is True
    assert response["response_token_estimate"] <= 2950, response["response_token_estimate"]
    _assert_target_response(response, L3_VLLM_ID, HBM_ID, outward=True)

    for unit_id in (L3_VLLM_ID, L4_ID):
        explained = repo.explain(
            refs=[repo.ref(unit_id, REVISIONS[unit_id])],
            conditions=OBSERVED,
            token_budget=3000,
        )
        assert explained["unresolved"] == [], (unit_id, explained["unresolved"])
        assert explained["response_token_estimate"] <= 2950, (unit_id, explained["response_token_estimate"])
        assert [item["id"] for item in explained["units"]] == [unit_id]
        requires = {item["id"]: item for item in explained["requires"]}
        assert HBM_ID in requires, (unit_id, list(requires))
        _assert_full_body(_units()[HBM_ID], requires[HBM_ID]["content"])
        assert requires[HBM_ID]["ready_to_apply"] is True


def test_l4_explore_context_reports_engine_duplication():
    index = RoutingIndex.from_root(CORPUS / "knowledge-vllm-ascend")
    response = index.query(
        "MLAPO mla_preprocess_only_decode wd_qkv wu_q static weight prefetch",
        conditions=OBSERVED,
        limit=1,
        token_budget=3000,
        mode="explore",
    )
    context = response["exploration_context"]
    mounted = {unit["id"]: unit for unit in context["mounted_units"]}
    assert L4_ID in mounted and HBM_ID in mounted
    assert token_estimate(mounted[L4_ID]["body"]) == token_estimate(_body(L4_ID))
    assert token_estimate(mounted[HBM_ID]["body"]) == token_estimate(_body(HBM_ID))
    assert response_token_estimate(response) > 3000
    assert response["usage"]["estimated_tokens"] < response_token_estimate(response)
    assert 4 * (token_estimate(_body(HBM_ID)) + token_estimate(_body(L4_ID))) > 2950


def test_tight_budget_and_unknown_parent_are_explicit(repos):
    repo = repos["knowledge-vllm-ascend"]
    tight = repo.query(
        "launch-to-sol inspect-launch sol feature-deltas mrv2 PCP",
        conditions=OBSERVED,
        limit=1,
        token_budget=1200,
    )
    row = tight["results"][0]
    assert row["id"] == LAUNCH_ID
    assert row["ready_to_apply"] is False
    assert row["excerpt_truncated"] is True
    assert any(item.get("status") == "budget_excluded" for item in row["inheritance_excluded"])

    missing = {key: value for key, value in OBSERVED.items() if key != "deployment"}
    explained = repo.explain(
        refs=[repo.ref(LAUNCH_ID, REVISIONS[LAUNCH_ID])],
        conditions=missing,
        token_budget=3000,
    )
    assert explained["units"] == [] and explained["requires"] == []
    unresolved = {item["id"]: item for item in explained["unresolved"]}
    assert LAUNCH_ID in unresolved and HBM_ID in unresolved
    assert unresolved[HBM_ID]["status"] == "condition_unknown"
    assert "deployment" in unresolved[HBM_ID]["missing_conditions"]
    assert explained["usage"]["body_token_estimate"] == 0


def test_version_capability_and_measurement_gear_mismatches(indexes):
    vllm, ops, dist, obs = (indexes[d] for d in DOMAINS)
    launch_query = "launch-to-sol inspect-launch sol feature-deltas mrv2 PCP"
    assert match_conditions(_units()[LAUNCH_ID].metadata["conditions"], {**OBSERVED, "cann": "10.0.0"}).status == "mismatch"
    assert not vllm.query(launch_query, conditions={**OBSERVED, "cann": "10.0.0"}, limit=1, token_budget=3000)["results"]
    assert not vllm.query(launch_query, conditions={**OBSERVED, "framework": {"vllm-ascend": "0.12.0"}}, limit=1, token_budget=3000)["results"]
    assert vllm.query(launch_query, conditions={**OBSERVED, "framework": {"vllm-ascend": "0.10.0"}}, limit=1, token_budget=3000)["results"][0]["id"] == LAUNCH_ID

    ep_query = "EP64 EPLB knee xPyD routing skew num_redundant_experts"
    assert not dist.query(ep_query, conditions={**PARALLEL_OBSERVED, "parallel_plan": {**PARALLEL_OBSERVED["parallel_plan"], "ep": 64}}, limit=1, token_budget=3000)["results"]
    assert not dist.query(ep_query, conditions={**PARALLEL_OBSERVED, "framework": {"vllm-ascend": "0.12.0"}}, limit=1, token_budget=3000)["results"]
    assert dist.query(ep_query, conditions=PARALLEL_OBSERVED, limit=1, token_budget=3000)["results"][0]["id"] == PARALLEL_ID
    no_capability = {key: value for key, value in PARALLEL_OBSERVED.items() if key != "runtime_capability"}
    assert not dist.query(ep_query, conditions=no_capability, limit=1, token_budget=3000)["results"]
    no_ledger = {key: value for key, value in PARALLEL_OBSERVED.items() if key != "hbm_ledger_checked"}
    assert not dist.query(ep_query, conditions=no_ledger, limit=1, token_budget=3000)["results"]

    abi_query = "inspect-op-abi repro-op FRACTAL_NZ format 29 named ABI binding"
    assert ABI_ID not in {row["id"] for row in ops.query(abi_query, conditions={**OBSERVED, "measurement_gear": "fast"}, limit=1, token_budget=3000)["results"]}
    diff_query = "diff-trace graph lowering host dispatch collective congestion"
    assert DIFF_ID not in {row["id"] for row in obs.query(diff_query, conditions={**OBSERVED, "measurement_gear": "fast"}, limit=1, token_budget=3000)["results"]}


def test_known_refs_dedup_feedback_and_domain_isolation(repos):
    repo = repos["knowledge-vllm-ascend"]
    store = repo.store
    target = repo.ref(LAUNCH_ID, REVISIONS[LAUNCH_ID])
    parent = repo.ref(HBM_ID, REVISIONS[HBM_ID])
    first = repo.explain(refs=[target], conditions=OBSERVED, token_budget=3000)
    assert first["unresolved"] == []
    assert LAUNCH_ID in {item["id"] for item in first["units"]}
    assert HBM_ID in {item["id"] for item in first["requires"]}
    assert first["usage"]["estimated_tokens"] <= 3000

    second = repo.explain(refs=[target], conditions=OBSERVED, known_refs=[parent], token_budget=3000)
    assert HBM_ID not in {item["id"] for item in second["requires"]}
    assert LAUNCH_ID in {item["id"] for item in second["units"]}
    assert second["usage"]["estimated_tokens"] < first["usage"]["estimated_tokens"]

    stale = repo.explain(refs=[target], conditions=OBSERVED, known_refs=[f"{HBM_ID}@99"], token_budget=3000)
    assert HBM_ID in {item["id"] for item in stale["requires"]}

    row = repo.query(
        "launch-to-sol inspect-launch sol feature-deltas mrv2 PCP",
        conditions=OBSERVED,
        limit=1,
        token_budget=3000,
    )["results"][0]
    assert row["feedback_ref"] == target
    assert row["ref"] == target
    summary = summarize_usage_events(store, LAUNCH_ID, REVISIONS[LAUNCH_ID], include_delivery=True)
    assert summary["query_deliveries"] >= 1
    repo.explain(refs=[target], conditions=OBSERVED, token_budget=3000)
    summary = summarize_usage_events(store, LAUNCH_ID, REVISIONS[LAUNCH_ID], include_delivery=True)
    assert summary["explain_deliveries"] >= 1

    legacy = repo.explain(target, conditions=OBSERVED, token_budget=3000)
    assert legacy["units"] == []
    assert any(
        item["id"] == LAUNCH_ID and item["status"] == "budget_excluded"
        for item in legacy["unresolved"]
    )

    with pytest.raises(ValueError, match="outside the selected domain"):
        repo.explain(f"mindie://operators/units/{LAUNCH_ID}@{REVISIONS[LAUNCH_ID]}")
    with pytest.raises(ValueError, match="revision was replaced"):
        repo.explain(repo.ref(LAUNCH_ID, 99))


def test_declarative_suite_per_domain(indexes):
    suites = {
        "knowledge-vllm-ascend": [
            {"id": "launch", "query": "launch-to-sol inspect-launch sol feature-deltas mrv2 PCP",
             "conditions": OBSERVED, "expected_unit_ids": [LAUNCH_ID],
             "require_foundation_baseline": [HBM_ID], "token_budget": 3000, "top_k": 1},
            {"id": "launch-cann10", "query": "launch-to-sol inspect-launch sol feature-deltas mrv2 PCP",
             "conditions": {**OBSERVED, "cann": "10.0.0"}, "expected_unit_ids": [],
             "forbidden_unit_ids": [LAUNCH_ID, HBM_ID], "token_budget": 3000, "top_k": 1},
            {"id": "mlapo-explore", "query": "MLAPO mla_preprocess_only_decode wd_qkv wu_q static weight prefetch",
             "conditions": OBSERVED, "mode": "explore", "expected_unit_ids": [L4_ID],
             "require_foundation_baseline": [HBM_ID],
             "forbidden_unit_ids": [L3_VLLM_ID], "token_budget": 3000, "top_k": 1},
        ],
        "knowledge-operators": [
            {"id": "varlen", "query": "aclnnFlashAttentionVarLenScore actual_seq_lens padding masked_varlen",
             "conditions": OBSERVED, "expected_unit_ids": [PAD_ID],
             "require_foundation_baseline": [PAD_ID],
             "forbidden_unit_ids": [ABI_ID], "token_budget": 3000, "top_k": 1},
            {"id": "abi", "query": "inspect-op-abi repro-op FRACTAL_NZ format 29 named ABI binding",
             "conditions": OBSERVED, "expected_unit_ids": [ABI_ID],
             "forbidden_unit_ids": [PAD_ID], "token_budget": 3000, "top_k": 1},
        ],
        "knowledge-infer-distributed": [
            {"id": "ep", "query": "EP64 EPLB knee xPyD routing skew num_redundant_experts",
             "conditions": PARALLEL_OBSERVED, "expected_unit_ids": [PARALLEL_ID],
             "forbidden_unit_ids": [LAUNCH_ID], "token_budget": 3000, "top_k": 1},
            {"id": "ep-infeasible", "query": "EP64 EPLB knee xPyD routing skew num_redundant_experts",
             "conditions": {**PARALLEL_OBSERVED, "parallel_plan": {**PARALLEL_OBSERVED["parallel_plan"], "ep": 64}},
             "expected_unit_ids": [], "forbidden_unit_ids": [PARALLEL_ID], "token_budget": 3000, "top_k": 1},
        ],
        "knowledge-infer-observability": [
            {"id": "diff", "query": "diff-trace graph lowering host dispatch collective congestion",
             "conditions": OBSERVED, "expected_unit_ids": [DIFF_ID],
             "require_foundation_baseline": [MEASURE_ID], "token_budget": 3000, "top_k": 1},
            {"id": "explore-no-l4", "query": "diff-trace graph lowering host dispatch collective congestion",
             "conditions": OBSERVED, "mode": "explore", "expected_unit_ids": [],
             "forbidden_unit_ids": [DIFF_ID, L4_ID], "token_budget": 3000, "top_k": 1},
        ],
    }
    for domain, cases in suites.items():
        report = evaluate_retrieval_suite(
            indexes[domain],
            cases,
            min_recall_at_k=1.0,
            min_mrr=1.0,
            min_foundation_baseline_rate=1.0,
            max_forbidden_leak_rate=0.0,
        )
        assert report["passed"] is True, (domain, report["failures"])
        assert report["recall_at_k"] == 1.0
        assert report["forbidden_leak_rate"] == 0.0


@pytest.mark.parametrize("domain", DOMAINS)
def test_real_corpus_overlay_builds_lints_and_queries(overlays, domain):
    overlay = overlays[domain]
    root, index = overlay["root"], overlay["index"]
    expected_units = {unit.id for unit in read_units(root)}
    assert DOMAIN_UNITS[domain] <= expected_units
    assert len(expected_units) >= len(DOMAIN_UNITS[domain])
    assert overlay["lint"]["valid"] is True
    assert build_routing_index(root, check=True)["ok"] is True

    for case_domain, query, expected, mode, conditions, parent in QUERY_CASES:
        if case_domain != domain:
            continue
        response = index.query(query, conditions=conditions, limit=1, token_budget=3000, mode=mode)
        _assert_target_response(response, expected, parent)
        assert response["usage"]["estimated_tokens"] <= 3000


@pytest.mark.parametrize(("domain", "query", "expected", "mode", "conditions", "parent"), QUERY_CASES)
def test_overlay_outward_repository_delivery_within_3000(
    overlay_repos, domain, query, expected, mode, conditions, parent
):
    response = overlay_repos[domain].query(
        query, conditions=conditions, limit=1, token_budget=3000, mode=mode
    )
    assert response["budget_met"] is True, (expected, response.get("minimum_tokens"))
    assert response["response_token_estimate"] <= 3000, response["response_token_estimate"]
    _assert_target_response(response, expected, parent, outward=True)


def test_overlay_explain_rule_and_cross_domain_boundary(overlays):
    vllm = overlays["knowledge-vllm-ascend"]
    target = _mindie("knowledge-vllm-ascend", LAUNCH_ID)
    explained = vllm["index"].explain_closure([target], conditions=OBSERVED, token_budget=3000)
    assert explained["unresolved"] == []
    assert LAUNCH_ID in {item["id"] for item in explained["units"]}
    assert HBM_ID in {item["id"] for item in explained["requires"]}
    _assert_full_body(
        _units()[LAUNCH_ID],
        next(item["content"] for item in explained["units"] if item["id"] == LAUNCH_ID),
    )
    _assert_full_body(
        _units()[HBM_ID],
        next(item["content"] for item in explained["requires"] if item["id"] == HBM_ID),
    )

    obs = overlays["knowledge-infer-observability"]
    response = obs["index"].query(
        "diff-trace graph lowering host dispatch collective congestion",
        conditions=OBSERVED,
        limit=1,
        token_budget=3000,
        mode="explore",
    )
    assert DIFF_ID in response["stripped_l3_ids"]
    delivered = {row["id"] for row in response["results"]}
    assert DIFF_ID not in delivered and L4_ID not in delivered
    context = response["exploration_context"]
    assert not (delivered & {DIFF_ID, L4_ID})
    assert L4_ID not in {unit["id"] for unit in context["mounted_units"]}
    assert L4_ID not in set(context["subagent_fact_pack"]["l4_mutator_ids"])
    assert L4_ID not in set(context["subagent_fact_pack"]["l1_foundation_ids"])
    assert all(not unit["id"].startswith("unit-ascend-day0") for unit in context["mounted_units"])


def test_overlay_negative_conditions_and_mismatch(overlays):
    vllm = overlays["knowledge-vllm-ascend"]["index"]
    launch_query = "launch-to-sol inspect-launch sol feature-deltas mrv2 PCP"
    for observed in (
        {**OBSERVED, "cann": "10.0.0"},
        {**OBSERVED, "framework": {"vllm-ascend": "0.12.0"}},
    ):
        assert LAUNCH_ID not in {
            row["id"]
            for row in vllm.query(launch_query, conditions=observed, limit=1, token_budget=3000)["results"]
        }
    missing = {key: value for key, value in OBSERVED.items() if key != "deployment"}
    explained = vllm.explain_closure(
        [_mindie("knowledge-vllm-ascend", LAUNCH_ID)], conditions=missing, token_budget=3000
    )
    assert explained["units"] == [] and explained["requires"] == []
    statuses = {item["id"]: item["status"] for item in explained["unresolved"]}
    assert statuses.get(LAUNCH_ID) == "condition_unknown"
    assert statuses.get(HBM_ID) == "condition_unknown"

    ops = overlays["knowledge-operators"]["index"]
    assert ABI_ID not in {
        row["id"]
        for row in ops.query(
            "inspect-op-abi repro-op FRACTAL_NZ format 29 named ABI binding",
            conditions={**OBSERVED, "measurement_gear": "fast"},
            limit=1,
            token_budget=3000,
        )["results"]
    }

    dist = overlays["knowledge-infer-distributed"]["index"]
    assert PARALLEL_ID not in {
        row["id"]
        for row in dist.query(
            "EP64 EPLB knee xPyD routing skew num_redundant_experts",
            conditions={**PARALLEL_OBSERVED, "parallel_plan": {**PARALLEL_OBSERVED["parallel_plan"], "ep": 64}},
            limit=1,
            token_budget=3000,
        )["results"]
    }
    assert PARALLEL_ID not in {
        row["id"]
        for row in dist.query(
            "EP64 EPLB knee xPyD routing skew num_redundant_experts",
            conditions={**PARALLEL_OBSERVED, "framework": {"vllm-ascend": "0.12.0"}},
            limit=1,
            token_budget=3000,
        )["results"]
    }

    obs = overlays["knowledge-infer-observability"]["index"]
    assert DIFF_ID not in {
        row["id"]
        for row in obs.query(
            "diff-trace graph lowering host dispatch collective congestion",
            conditions={**OBSERVED, "measurement_gear": "fast"},
            limit=1,
            token_budget=3000,
        )["results"]
    }


def test_outward_overlay_complete_reuse_and_negative_conditions(overlay_repos):
    repo = overlay_repos["knowledge-vllm-ascend"]
    query = "MLAPO mla_preprocess_only_decode wd_qkv wu_q static weight prefetch"
    response = repo.query(query, conditions=OBSERVED, limit=1, token_budget=3000)
    assert response["budget_met"] is True
    assert response["response_token_estimate"] <= 2950, response["response_token_estimate"]
    _assert_target_response(response, L3_VLLM_ID, HBM_ID, outward=True)

    explained = repo.explain(
        refs=[repo.ref(L4_ID, REVISIONS[L4_ID])], conditions=OBSERVED, token_budget=3000
    )
    assert explained["unresolved"] == []
    assert explained["response_token_estimate"] <= 2950
    assert [item["id"] for item in explained["units"]] == [L4_ID]
    requires = {item["id"]: item for item in explained["requires"]}
    assert HBM_ID in requires
    _assert_full_body(_units()[HBM_ID], requires[HBM_ID]["content"])

    missing = {key: value for key, value in OBSERVED.items() if key != "deployment"}
    negative = repo.explain(
        refs=[repo.ref(L4_ID, REVISIONS[L4_ID])], conditions=missing, token_budget=3000
    )
    assert negative["units"] == [] and negative["requires"] == []
    unresolved = {item["id"]: item for item in negative["unresolved"]}
    assert unresolved[L4_ID]["status"] == "condition_unknown"
    assert unresolved[HBM_ID]["status"] == "condition_unknown"
    assert "deployment" in unresolved[HBM_ID]["missing_conditions"]

    for version in ("0.12.0", "0.13.0"):
        observed = {**OBSERVED, "framework": {"vllm-ascend": version}}
        delivered = {
            row["id"]
            for row in repo.query(query, conditions=observed, limit=3, token_budget=3000)["results"]
        }
        assert not (delivered & {L3_VLLM_ID, L4_ID, HBM_ID}), (version, delivered)
        mismatch = repo.explain(
            refs=[repo.ref(L3_VLLM_ID, REVISIONS[L3_VLLM_ID])],
            conditions=observed,
            token_budget=3000,
        )
        statuses = {item["id"]: item["status"] for item in mismatch["unresolved"]}
        assert statuses.get(HBM_ID) == "condition_mismatch", (version, statuses)

    distributed = overlay_repos["knowledge-infer-distributed"]
    ep_query = "EP64 EPLB knee xPyD routing skew num_redundant_experts"
    assert PARALLEL_ID not in {
        row["id"]
        for row in distributed.query(
            ep_query,
            conditions={**PARALLEL_OBSERVED, "framework": {"vllm-ascend": "0.12.0"}},
            limit=3,
            token_budget=3000,
        )["results"]
    }
    eager_obs = {**PARALLEL_OBSERVED, "execution_mode": "eager"}
    assert PARALLEL_ID not in {
        row["id"]
        for row in distributed.query(ep_query, conditions=eager_obs, limit=3, token_budget=3000)["results"]
    }
    eager_exp = distributed.explain(
        refs=[distributed.ref(PARALLEL_ID, REVISIONS[PARALLEL_ID])],
        conditions=eager_obs,
        token_budget=3000,
    )
    assert eager_exp["units"] == []
    assert {item["id"]: item["status"] for item in eager_exp["unresolved"]}[PARALLEL_ID] == "condition_mismatch"
    no_exec_obs = {k: v for k, v in PARALLEL_OBSERVED.items() if k != "execution_mode"}
    assert PARALLEL_ID not in {
        row["id"]
        for row in distributed.query(ep_query, conditions=no_exec_obs, limit=3, token_budget=3000)["results"]
    }
    no_exec_exp = distributed.explain(
        refs=[distributed.ref(PARALLEL_ID, REVISIONS[PARALLEL_ID])],
        conditions=no_exec_obs,
        token_budget=3000,
    )
    assert no_exec_exp["units"] == []
    unresolved_par = {item["id"]: item for item in no_exec_exp["unresolved"]}[PARALLEL_ID]
    assert unresolved_par["status"] == "condition_unknown"
    assert "execution_mode" in unresolved_par["missing_conditions"]
    assert (
        distributed.query(ep_query, conditions=PARALLEL_OBSERVED, limit=1, token_budget=3000)["results"][0]["id"]
        == PARALLEL_ID
    )
