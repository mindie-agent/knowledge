"""[Role] Validate the modeling, profiling, and tuning graded knowledge units.
[Invariants] Each domain corpus builds an independent RoutingIndex without cross-domain pooling; all units satisfy mindie-graded-unit/1 schema, provenance digest, and <= 3,000-token bounds.
[Timeouts & Locks] Bounded local file reads and optional read-only git cat-file checks (< 5s); no sleeps or persistent locks.
[Side-Effect Policy] Read-only against corpus/ and reference repositories; writes index verification outputs exclusively under pytest tmp_path.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from mindie_knowledge.graded.ablation import verified_v2
from mindie_knowledge.graded.conditions import match_conditions
from mindie_knowledge.graded.index import RoutingIndex, read_units, token_estimate
from mindie_knowledge.graded.maintenance import build_routing_index, lint_corpus, shard_plan
from mindie_knowledge.graded.units import TIER_CATEGORIES

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus"
DOMAINS = (
    "knowledge-vllm-ascend",
    "knowledge-operators",
    "knowledge-infer-distributed",
    "knowledge-infer-observability",
)
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
UNIT_IDS = set().union(*DOMAIN_UNITS.values())
L1_IDS = {
    "unit-ascend-aclgraph-bucket-padding-and-varlen-axioms",
    "unit-vllm-ascend-hbm-memory-hidden-assassins-axioms",
    "unit-cann9-steady-state-capture-and-clock-hygiene",
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
EVIDENCE_STRENGTHS = {"hardware_spec", "reproduced_empirical", "single_observation", "hypothesis"}

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

def _local_repos() -> dict[str, Path]:
    raw = os.environ.get("MINDIE_LOCAL_PROVENANCE_ROOTS", "").strip()
    configured = {k: Path(v) for k, v in json.loads(raw).items()} if raw else {}
    return {
        "vllm-project/vllm-ascend": configured.get("vllm-project/vllm-ascend", ROOT / ".missing-ref/vllm-ascend"),
        "knowledge-infer-observability": configured.get(
            "knowledge-infer-observability", ROOT / ".missing-ref/knowledge-infer-observability"
        ),
        "knowledge": configured.get("knowledge", ROOT),
        "mindie-profiler": configured.get("mindie-profiler", ROOT / ".missing-ref/profiler"),
        "nr_s1_launch_sol": configured.get("nr_s1_launch_sol", ROOT / ".missing-ref/nr_s1_launch_sol"),
        "next-release-design": configured.get("next-release-design", ROOT / ".missing-ref/next-release-design"),
    }


LOCAL_REPOS = _local_repos()
EXTERNAL_UNVERIFIED = {"cann/ops-transformer", "gitcode.com/cann/ops-transformer"}
EXAMPLE = (
    CORPUS
    / "knowledge-vllm-ascend"
    / "vllm-ascend"
    / "performance-modeling"
    / "mlapo-ablation"
    / "ablation-receipt-example.json"
)


def _units(domain: str | None = None):
    return {unit.id: unit for unit in read_units(CORPUS / domain if domain else CORPUS)}


def _body(unit_id: str) -> str:
    return _units()[unit_id].body


def _git(root: Path, *args: str, binary: bool = False):
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=not binary,
        env=env,
        check=False,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def indexes():
    return {domain: RoutingIndex.from_root(CORPUS / domain) for domain in DOMAINS}


def test_every_unit_is_valid_and_bound_to_its_domain_path():
    units = _units()
    assert set(units) == UNIT_IDS
    assert len(units) == 9
    for unit in units.values():
        raw = (CORPUS / unit.path).read_bytes()
        assert b"\r" not in raw, f"CR byte in {unit.path}"
        assert unit.path.endswith(f"/{unit.id}.md")
        domain = PurePosixPath(unit.path).parts[0]
        assert domain in DOMAINS
        relative_parent = PurePosixPath(unit.path).relative_to(domain).parent.as_posix()
        assert relative_parent == unit.metadata["domain_path"], unit.id
        assert unit.metadata["category"] in TIER_CATEGORIES[unit.metadata["tier"]]
        assert unit.metadata["revision"] == REVISIONS[unit.id]
        assert unit.metadata["revision"] >= 2, unit.id
        strength = unit.metadata["evidence_strength"]
        assert strength in EVIDENCE_STRENGTHS, (unit.id, strength)
        assert strength != "software_contract", unit.id
        if unit.metadata["tier"] == "L1":
            assert strength in {"hardware_spec", "reproduced_empirical"}, unit.id
        assert token_estimate(unit.body) <= 3000, unit.id
        provenance = unit.metadata["provenance"]
        assert provenance["source_type"] and provenance["source_locator"]
        assert re.fullmatch(r"[a-f0-9]{64}", provenance["source_digest"]), unit.id
        assert provenance["evidence_refs"], unit.id
        assert all("@" in ref for ref in provenance["evidence_refs"]), unit.id
        if unit.metadata["tier"] == "L4":
            assert provenance.get("experiment_status") == "not_run_source_only"
    assert L1_IDS <= set(units)


def test_corpus_bodies_cover_the_reviewed_contracts():
    bodies = {ident: _body(ident) for ident in UNIT_IDS}
    pad, abi = bodies[PAD_ID], bodies[ABI_ID]
    assert "actualSequenceLengthQ[-1]" in pad
    assert "M_b - M_l" in pad and "dummy request" in pad
    assert "must equal the physical packed query total `T`" in pad
    assert "[T1, N2, sparseCount]" in pad and "[B, S1, N2, sparseCount]" in pad
    assert "nonnegative" in pad and "compress_ratio" in pad
    assert "BSH" in pad and "BSND" in pad and "Paged KV" in pad
    assert "paged TND" in pad and "per-request KV lengths" in pad
    assert "only `TND`" in pad
    assert "never `aclnnFlashAttentionVarLenScore`" in pad
    assert "ceil((M_b - M_l) / q_max)" in pad
    assert "q_len in [1, 16]" in pad and "31 = 16 + 15" in pad
    assert "never one 31-token request" in pad
    assert "non-`TND` per-batch split" not in pad

    assert "ABIInspection" in abi and "--occurrence" in abi and "--cann-version" in abi
    assert "--help" in abi and "unavailable" in abi
    assert "ceil(D / C0)" in abi and "ceil(N / 16)" in abi
    assert "[batch, ceil(D/C0), ceil(N/16)*16, C0]" in abi
    assert "[1, 2, 32, 16]" in abi and "never `[1, 32, 32]`" in abi
    assert "1024 elements (2048 bytes)" in abi and "not 544" in abi
    assert "format_mismatch" in abi and "varlen_invariant" in abi and "slot_misalignment" in abi
    assert "--op-id" not in abi

    launch = bodies[LAUNCH_ID]
    assert "launch-parse/1" in launch and "sol-comparison/1" in launch
    assert "--scope model" in launch and "counterfactuals_additive" in launch
    assert "context_parallel.md@175ef4d" in launch and "not** supported" in launch
    assert "--prefill-context-parallel-size" in launch and "--decode-context-parallel-size" in launch
    assert "t_math" in launch and "delta_pct" in launch
    assert "mindie-sim sol --help" in launch

    hbm = bodies[HBM_ID]
    assert "[start, end)" in hbm and "peak_live" in hbm and "9 GiB" in hbm
    assert "memory_reserved - memory_allocated" in hbm
    assert "6-16 GiB" not in hbm and "6–16 GiB" not in hbm
    assert "max_t sum(active_workspace_bytes(t))" in hbm
    assert "PCP" in hbm and "does not shrink the persistent decode pool" in hbm
    assert 650 <= token_estimate(hbm) <= 720, token_estimate(hbm)

    measure = bodies[MEASURE_ID]
    for token in (
        "min_warmup_steps",
        "steady_window_steps",
        "max_cv",
        "15.0",
        "1.5x",
        "aclrtSynchronizeStream",
        "no implemented MAD/P50",
        "hbm_malloc_churn_during_window",
    ):
        assert token in measure, token
    assert "outlier" in measure
    assert "(stdev/mean) * 1_000_000 <= max_cv_ppm" in measure
    assert "571,428 ppm" in measure

    diff = bodies[DIFF_ID]
    for token in (
        "O = R0 + d_graph + d_host + d_comm + d_op",
        "union(NPU-idle",
        "host_bound_ratio",
        "max(abs_noise_floor",
        "signed",
        "cross-rank clocks",
        "R4_model",
        "unexplained",
        "--prediction prediction.json",
    ):
        assert token in diff, token

    parallel = bodies[PARALLEL_ID]
    for token in (
        "W = PP * PCP * DP * TP",
        "EP <= DP * TP",
        "EP=16",
        "64 > 16",
        "KV_D = ISL + OSL",
        "mu_D = B_D * g",
        "min(N_P * mu_P, N_D * mu_D)",
        "rho < 1",
        "eta_skew     = max_r n_r / mean",
        "mean_service",
        "beta*K > alpha*(g-1)",
        "bytes / 1000",
        "g   = 1 + sum",
        "DSA-CP",
        "dsa_cp+dcp",
        "sweep-ep-knee",
        "fabric-probe",
        "parked",
        "mindie://vllm-ascend/units/unit-vllm-ascend-hbm-memory-hidden-assassins-axioms@2",
    ):
        assert token in parallel, token

    mutator, protocol = bodies[L4_ID], bodies[L3_VLLM_ID]
    assert "mla_preprocess_only_decode" in mutator and "wd_qkv" in mutator
    assert "minus_A..minus_D" in mutator and "ablation.verified_v2" in mutator
    assert "unit-vllm-ascend-mlapo-prefetch-ablation-protocol`@3" in mutator
    assert 280 <= token_estimate(mutator) <= 340, token_estimate(mutator)
    assert "mla_prolog_rope_kvcache_step" not in mutator
    for token in (
        "baseline",
        "full_candidate",
        "minus_A",
        "minus_B",
        "minus_C",
        "minus_D",
        "final_combination",
        "host_monotonic_ns",
        "buffer_sha256",
        "max_relative_loss",
        "optimize-graph",
        "verified_v2",
        "unsupported_prefetch_path",
    ):
        assert token in protocol, token
    assert 1050 <= token_estimate(protocol) <= 1150, token_estimate(protocol)


def test_inheritance_closure_and_conditions_are_aligned():
    units = _units()
    assert units[L4_ID].metadata["inherits_from"] == [HBM_ID]
    assert units[L3_VLLM_ID].metadata["inherits_from"] == [HBM_ID]
    hbm, parallel = units[HBM_ID], units[PARALLEL_ID]
    assert hbm.metadata["conditions"]["framework"] == parallel.metadata["conditions"]["framework"]
    for observed in (
        {**OBSERVED, "framework": {"vllm-ascend": "0.12.0"}},
        {**OBSERVED, "framework": {"vllm-ascend": "0.13.0"}},
    ):
        assert match_conditions(hbm.metadata["conditions"], observed).status == "mismatch"
        assert match_conditions(parallel.metadata["conditions"], observed).status == "mismatch"
    assert match_conditions(hbm.metadata["conditions"], OBSERVED).status == "matched"
    assert match_conditions(parallel.metadata["conditions"], PARALLEL_OBSERVED).status == "matched"
    hbm_ref = f"unit-vllm-ascend-hbm-memory-hidden-assassins-axioms@{REVISIONS[HBM_ID]}"
    assert f"{hbm_ref}`" in units[L4_ID].body
    assert f"{hbm_ref}`" in units[L3_VLLM_ID].body
    assert f"{hbm_ref}`" in units[PARALLEL_ID].body
    assert f"unit-vllm-ascend-mlapo-prefetch-ablation-protocol`@{REVISIONS[L3_VLLM_ID]}" in units[L4_ID].body
    assert f"unit-ascend-aclgraph-bucket-padding-and-varlen-axioms`@{REVISIONS[PAD_ID]}" in units[ABI_ID].body


def test_varlen_layouts_and_qmax_chunking_are_explicit():
    pad = _body(PAD_ID)
    assert "`BSH` / `BSND` / `BNSD`" in pad
    assert "`aclnnFusedInferAttentionScoreV2`/`V3` and `aclnnFlashAttentionScore`" in pad
    assert "Supports **only `TND`**" in pad
    assert "there is no `BSH`/`BSND`/`BNSD` overload" in pad
    assert "partition the padded tail `M_b - M_l`" in pad
    assert "each at most the overload's per-request `q_max`" in pad

    def _chunk_tail(tail: int, q_max: int = 16) -> list[int]:
        if tail <= 0:
            return []
        n = math.ceil(tail / q_max)
        full = tail // q_max
        rem = tail % q_max
        chunks = [q_max] * full + ([rem] if rem else [])
        assert len(chunks) == n and sum(chunks) == tail and max(chunks) <= q_max
        return chunks

    assert _chunk_tail(0) == []
    assert _chunk_tail(1) == [1]
    assert _chunk_tail(15) == [15]
    assert _chunk_tail(16) == [16]
    assert _chunk_tail(17) == [16, 1]
    assert _chunk_tail(31) == [16, 15]
    assert _chunk_tail(32) == [16, 16]
    assert _chunk_tail(33) == [16, 16, 1]


def test_atb_fractal_nz_descriptor_is_rank_four():
    abi = _body(ABI_ID)
    assert "storage = [ceil(D / C0), ceil(N / 16), 16, C0]" in abi
    assert "[batch, ceil(D/C0), ceil(N/16)*16, C0]" in abi
    assert "[1, 2, 32, 16]" in abi
    assert "1,024 elements" in abi
    assert "never `[1, 32, 32]`" in abi


def test_max_cv_ppm_scaling_boundary():
    samples = [1, 1, 1, 1, 3]
    mean = statistics.mean(samples)
    stdev = statistics.pstdev(samples)
    ppm = int((stdev / mean) * 1_000_000)
    assert mean == 1.4 and stdev == 0.8
    assert ppm == 571_428
    assert ppm > 150_000
    assert (0.15 * 1_000_000) <= 150_000
    assert (0.150001 * 1_000_000) > 150_000
    assert (stdev / mean) <= 150_000
    assert "(stdev/mean) * 1_000_000 <= max_cv_ppm" in _body(MEASURE_ID)


def test_aiv_tile_loop_and_prefetch_fallback_are_kept():
    protocol, mutator = _body(L3_VLLM_ID), _body(L4_ID)
    assert "ceil(T_bucket / T_tile) <= available AIV cores" not in protocol
    assert "`ceil(T_bucket/T_tile)` may exceed the core count" in protocol
    assert "`launched_blocks <= available_aiv_cores`" in protocol
    assert "usable_ub_bytes" in protocol
    for body in (protocol, mutator):
        assert "foreground Vector chain" in body
        assert "Cube input" in body
        assert "unsupported_prefetch_path" in body
        assert "keep the original graph" in body


def test_evidence_refs_resolve_to_local_commits_and_file_bytes():
    missing_repos = [name for name, path in LOCAL_REPOS.items() if not path.exists()]
    if missing_repos:
        pytest.skip(f"local reference repositories not mounted: {missing_repos}")
    units = _units()
    for unit in units.values():
        provenance = unit.metadata["provenance"]
        for reference in [provenance["source_locator"], *provenance["evidence_refs"]]:
            match = re.fullmatch(r"([^@]+)@([^:]+)(?::(.+))?", reference)
            assert match, (unit.id, reference)
            repository, revision, path = match.groups()
            if repository in EXTERNAL_UNVERIFIED:
                assert repository.endswith("cann/ops-transformer") and path
                continue
            root = LOCAL_REPOS[repository]
            path = path.split("#", 1)[0] if path else None
            if re.fullmatch(r"[0-9a-f]{7,64}", revision):
                commit = _git(root, "cat-file", "-e", f"{revision}^{{commit}}")
                assert commit.returncode == 0, (unit.id, repository, revision, commit.stderr)
                if not path:
                    continue
                tracked = _git(root, "cat-file", "-e", f"{revision}:{path}")
                if tracked.returncode == 0:
                    blob = _git(root, "show", f"{revision}:{path}", binary=True).stdout
                    if reference == provenance["source_locator"]:
                        assert _sha256(blob) == provenance["source_digest"], (unit.id, reference)
                else:
                    file_path = root / path
                    assert file_path.is_file(), (unit.id, file_path)
                    if reference == provenance["source_locator"]:
                        assert _sha256(file_path.read_bytes()) == provenance["source_digest"]
                continue
            assert re.fullmatch(r"worktree-snapshot-\d{8}|\d{4}-\d{2}-\d{2}", revision), (unit.id, revision)
            assert path
            file_path = root / path
            assert file_path.is_file(), (unit.id, file_path)
            if reference == provenance["source_locator"]:
                assert _sha256(file_path.read_bytes()) == provenance["source_digest"], (unit.id, reference)


def test_launch_and_observability_provenance_identities():
    units = _units()
    expected_engine_rev = "d5c7f01718bd508906d0eef1227b43697fa77912"
    assert _git(ROOT, "cat-file", "-e", f"{expected_engine_rev}^{{commit}}").returncode == 0
    protocol_refs = units[L3_VLLM_ID].metadata["provenance"]["evidence_refs"]
    assert any(
        ref.startswith(f"knowledge@{expected_engine_rev}:mindie_knowledge/graded/ablation.py")
        for ref in protocol_refs
    )
    expected_obs_rev = "4b3dd86b365c9e588a1eb47df7dcc6437fdf8a89"
    diff_refs = units[DIFF_ID].metadata["provenance"]["evidence_refs"]
    assert any(ref.startswith(f"knowledge-infer-observability@{expected_obs_rev}:") for ref in diff_refs)
    assert not any("knowledge-infer-observability@d5c7f017" in ref for ref in diff_refs)
    assert units[LAUNCH_ID].metadata["provenance"]["source_digest"] == (
        "6f7b7424d88920ed3dfb539f635dbc4d3d01cc36b0fae744856b99318854c1f9"
    )
    feature_reports = LOCAL_REPOS["nr_s1_launch_sol"] / "mindie_sim" / "feature_reports.py"
    obs_root = LOCAL_REPOS["knowledge-infer-observability"]
    if not (feature_reports.is_file() and obs_root.exists()):
        return
    assert _sha256(feature_reports.read_bytes()) == units[LAUNCH_ID].metadata["provenance"]["source_digest"]
    assert _git(obs_root, "cat-file", "-e", f"{expected_obs_rev}^{{commit}}").returncode == 0
    blob = _git(engine_root, "show", f"{engine_head}:mindie_knowledge/graded/ablation.py", binary=True).stdout
    assert _sha256(blob) == "d93d6ac3f0270af479fb355bd0e687e15c6bffcdb254f161493424f2af17b711"


def test_protocol_receipt_schema_and_example_validate():
    raw = EXAMPLE.read_bytes()
    example = json.loads(raw)
    protocol = _body(L3_VLLM_ID)
    assert "samples_ms" in protocol and "run-level" in protocol
    assert "{shape_sha256: [buffer_sha256, ...]}" in protocol
    assert _sha256(raw) in protocol
    assert "ablation-receipt-example.json" in protocol
    assert isinstance(example["runs"], list) and len(example["runs"]) == 3
    for run in example["runs"]:
        assert "samples_ms" in run and "samples_ms" not in run["timing"]
        assert isinstance(run["timing"]["buffer_sha256"], dict)
        assert all(isinstance(value, list) for value in run["timing"]["buffer_sha256"].values())
    assert verified_v2(example, example["runs"], example["final"]) is True

    for position in range(len(example["runs"])):
        mutated = copy.deepcopy(example)
        timing = mutated["runs"][position]["timing"]
        shape = next(iter(timing["buffer_sha256"]))
        timing["buffer_sha256"] = timing["buffer_sha256"][shape]
        assert verified_v2(mutated, mutated["runs"], mutated["final"]) is False

    moved = copy.deepcopy(example)
    moved["runs"][0]["timing"]["samples_ms"] = moved["runs"][0].pop("samples_ms")
    assert verified_v2(moved, moved["runs"], moved["final"]) is False


@pytest.mark.parametrize("domain", DOMAINS)
def test_each_mirror_rebuilds_checks_and_lints(domain, tmp_path):
    root = CORPUS / domain
    check = build_routing_index(root, check=True)
    assert check["ok"] and not check["written"], check["errors"]
    assert check["unit_count"] == len(DOMAIN_UNITS[domain])
    report = lint_corpus(root)
    assert report["valid"], report["errors"]
    assert report["counts"]["units"] == len(DOMAIN_UNITS[domain])
    assert not [c for c in shard_plan(read_units(root))["candidates"]]
    out = tmp_path / domain / "index"
    write = build_routing_index(root, index_dir=out)
    assert write["ok"] and write["written"], write["errors"]
    for name in ("routing-manifest.json", "shard-topology.json"):
        written = json.loads((out / name).read_text(encoding="utf-8"))
        checked = json.loads((root / "index" / name).read_text(encoding="utf-8"))
        for timing in ("build_elapsed_ms", "index_build_elapsed_ms"):
            written.pop(timing, None)
            checked.pop(timing, None)
        assert written == checked, name


def test_domain_indexes_are_separate_and_do_not_pool(indexes):
    for domain, index in indexes.items():
        assert {unit.id for unit in index.units.values()} == DOMAIN_UNITS[domain]
        for ident in DOMAIN_UNITS[domain]:
            assert set(index.closure[ident]) <= DOMAIN_UNITS[domain]
    vllm = {unit.id for unit in indexes["knowledge-vllm-ascend"].units.values()}
    obs = {unit.id for unit in indexes["knowledge-infer-observability"].units.values()}
    assert L4_ID in vllm and L4_ID not in obs
    assert DIFF_ID in obs and DIFF_ID not in vllm
    assert HBM_ID in indexes["knowledge-vllm-ascend"].closure[L4_ID]
