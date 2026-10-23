"""Live NPU cluster (A2 & A3) end-to-end integration test for the knowledge loop.

Connects via SSH to real Ascend A2 (173.125.1.2, container mindie-bench-a2-core-20261017)
and A3 (192.168.13.165, container vaws-offload-pd165-20261016) environments, captures
real NPU hardware/CANN topology and operator troubleshooting logs, and verifies:
  TranscriptCapture -> Redaction (IPv4 / private path masking + Gitleaks) ->
  MaterialStore chunked persistence -> ReMe BM25 technical tokenizer recall.

The S1/S2 multi-Die case additionally reads each A3 node's per-NPU Chip Count,
idle Phy-ID pairs and SIO/HCCS matrix before querying the real corpora.
"""
from __future__ import annotations

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

import pytest

from conftest import make_admission, write_settings
from lane_support import load_parser
from material_worker_fixture import package_body
from mindie_knowledge.graded.feedback import record_usage_event, summarize_usage_events
from mindie_knowledge.graded.maintenance import load_corpus
from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_redaction import install_scanner

pytestmark = pytest.mark.live_npu


A2_HOST = "173.125.1.2"
A2_CONTAINER = "mindie-bench-a2-core-20261017"
A3_HOST = "192.168.13.165"
A3_CONTAINER = "vaws-offload-pd165-20261016"
A3_SECOND_HOST = "192.168.13.156"
_CONNECTED = "MINDIE_NPU_PROBE_CONNECTED"
_NPU_INFO_SECTION = "__MINDIE_NPU_INFO__"
_NPU_TOPO_SECTION = "__MINDIE_NPU_TOPO__"


def _live_targets() -> dict[str, tuple[str, str]]:
    return {
        "a2": (
            os.environ.get("MINDIE_LIVE_A2_HOST", "").strip() or A2_HOST,
            os.environ.get("MINDIE_LIVE_A2_CONTAINER", "").strip() or A2_CONTAINER,
        ),
        "a3": (
            os.environ.get("MINDIE_LIVE_A3_HOST", "").strip() or A3_HOST,
            os.environ.get("MINDIE_LIVE_A3_CONTAINER", "").strip() or A3_CONTAINER,
        ),
    }


def _ssh_exec(host: str, remote_command: str, *, timeout: int = 15) -> str:
    cmd = [
        "ssh",
        "-n",
        "-o", "ControlMaster=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        f"root@{host}",
        f"printf '{_CONNECTED}\\n' >&2; {remote_command}",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if _CONNECTED in stderr:
            pytest.fail(f"Live NPU host {host} remote command timed out after SSH connected")
        pytest.skip(f"Live NPU host {host} unreachable within {timeout}s: {exc}")
    except TimeoutError:
        raise
    except OSError as exc:
        pytest.skip(f"Live NPU host {host} unreachable within {timeout}s: {exc}")
    if res.returncode != 0:
        detail = res.stderr.replace(_CONNECTED, "").strip()
        if res.returncode == 255 and _CONNECTED not in res.stderr:
            pytest.skip(f"Live NPU host {host} SSH unavailable: {detail}")
        pytest.fail(f"Live NPU host {host} remote command returned {res.returncode}: {detail}")
    return res.stdout.strip()


def _ssh_probe(host: str, container: str) -> str:
    quoted_container = shlex.quote(container)
    return _ssh_exec(
        host,
        (
            f"npu-smi info -l && "
            f"docker exec {quoted_container} sh -c "
            f"'ls -d /usr/local/Ascend/* 2>/dev/null; uname -m'"
        ),
    )


def _ssh_topology_probe(host: str) -> str:
    """Read per-NPU Chip Count, Phy-ID rows, processes and the HCCS matrix."""
    return _ssh_exec(
        host,
        (
            "npu-smi info -l; "
            f"printf '\\n{_NPU_INFO_SECTION}\\n'; "
            "npu-smi info; "
            f"printf '\\n{_NPU_TOPO_SECTION}\\n'; "
            "npu-smi info -t topo"
        ),
        timeout=20,
    )


_TOTAL_COUNT = re.compile(r"Total Count\s*:\s*(\d+)")
_NPU_ID_LINE = re.compile(r"NPU ID\s*:\s*(\d+)")
_CHIP_COUNT = re.compile(r"Chip Count\s*:\s*(\d+)")
_NPU_ROW = re.compile(r"^\|\s*(\d+)\s+Ascend\S*\s*\|")
_CHIP_ROW = re.compile(r"^\|\s*(\d+)\s+(\d+)\s+\|\s*[0-9A-Fa-f]{4}:")
_PROCESS_ROW = re.compile(r"^\|\s*(\d+)\s+(\d+)\s+\|\s*(\d+)\s+\|\s*([A-Za-z][A-Za-z0-9_.-]*)")
_IDLE_NPU = re.compile(r"No running processes found in NPU\s+(\d+)")
_TOPO_PHY_ID = re.compile(r"Phy-ID(\d+)")


@dataclass(frozen=True)
class _NpuTopology:
    host: str
    chip_counts: dict
    phy_ids: dict
    processes: dict
    idle_npus: frozenset
    occupied_npus: frozenset
    links: dict

    @property
    def total_npus(self) -> int:
        return len(self.chip_counts)

    @property
    def total_dies(self) -> int:
        return sum(self.chip_counts.values())

    def idle_pairs(self):
        pairs = []
        for npu in sorted(self.idle_npus):
            if npu % 2 == 0 and npu + 1 in self.idle_npus:
                phys = tuple(sorted((*self.phy_ids[npu], *self.phy_ids[npu + 1])))
                pairs.append((npu, npu + 1, phys))
        return tuple(pairs)


def _parse_topo_links(topology: str) -> dict:
    lines = [line.strip() for line in topology.splitlines() if line.strip()]
    header = next((line for line in lines if line.startswith("Phy-ID")), None)
    if header is None:
        return {}
    order = [int(value) for value in _TOPO_PHY_ID.findall(header)]
    links = {}
    for line in lines:
        if line == header:
            continue
        parts = line.split()
        if not parts or not re.fullmatch(r"Phy-ID\d+", parts[0]) or len(parts) < len(order) + 1:
            continue
        source = int(parts[0][len("Phy-ID"):])
        for target, token in zip(order, parts[1:]):
            if source != target:
                links[(source, target)] = token
    return links


def _parse_npu_topology(host: str, output: str) -> _NpuTopology:
    if _NPU_INFO_SECTION not in output or _NPU_TOPO_SECTION not in output:
        pytest.fail(f"Live NPU host {host} topology probe returned an incomplete receipt")
    listing, remainder = output.split(_NPU_INFO_SECTION, 1)
    info, topology = remainder.split(_NPU_TOPO_SECTION, 1)

    chip_counts, current = {}, None
    for line in listing.splitlines():
        match = _NPU_ID_LINE.search(line)
        if match:
            current = int(match.group(1))
            continue
        match = _CHIP_COUNT.search(line)
        if match and current is not None:
            chip_counts[current] = int(match.group(1))
    total = _TOTAL_COUNT.search(listing)
    if total is None or len(chip_counts) != int(total.group(1)):
        pytest.fail(f"Live NPU host {host} did not report every NPU Chip Count")

    phy_ids, processes, current = {}, {}, None
    for line in info.splitlines():
        match = _NPU_ROW.match(line)
        if match:
            current = int(match.group(1))
            continue
        match = _CHIP_ROW.match(line)
        if match and current is not None:
            phy_ids.setdefault(current, []).append(int(match.group(2)))
            continue
        match = _PROCESS_ROW.match(line)
        if match:
            processes.setdefault(int(match.group(1)), []).append(match.group(4))

    if set(phy_ids) != set(chip_counts) or any(
            len(phy_ids[npu]) != count for npu, count in chip_counts.items()):
        pytest.fail(f"Live NPU host {host} Phy-ID rows disagree with Chip Count")
    idle = frozenset(int(value) for value in _IDLE_NPU.findall(info))
    occupied = frozenset(processes)
    if idle & occupied or idle | occupied != frozenset(chip_counts):
        pytest.fail(f"Live NPU host {host} process occupancy is incomplete or contradictory")
    return _NpuTopology(
        host=host,
        chip_counts=chip_counts,
        phy_ids={npu: tuple(sorted(values)) for npu, values in phy_ids.items()},
        processes={npu: tuple(names) for npu, names in processes.items()},
        idle_npus=idle,
        occupied_npus=occupied,
        links=_parse_topo_links(topology),
    )


def _live_corpus(name: str):
    from test_corpus_deliverables import CORPORA
    root = CORPORA[name]["root"]
    if not root.is_dir():
        pytest.skip(f"external corpus checkout not present: {root}")
    units, index, errors = load_corpus(Path(root))
    if errors or index is None or not units:
        pytest.fail(f"live {name} corpus is invalid: {errors}")
    return index


def _append_turn(path, role: str, text: str) -> None:
    content_type = "input_text" if role == "user" else "output_text"
    record = {
        "type": "response_item",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": content_type, "text": text}],
        },
    }
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _live_graded_check(store, probe_text: str, *, label: str):
    """Install and exercise one probe-bound local unit; never claim performance."""
    cann = re.search(r"/Ascend/cann-(\d+(?:\.\d+)+)", probe_text)
    arch = next((name for name in ("aarch64", "x86_64") if name in probe_text), None)
    if cann is None or arch is None:
        return None
    cann_version = cann.group(1)
    ident = f"unit-live-{label}-topology-probe"
    live_unit = GradedUnit.from_parts(
        {
            "schema": "mindie-graded-unit/1",
            "id": ident,
            "revision": 1,
            "tier": "L2",
            "category": "sop",
            "domain_path": f"vllm-ascend/runtime/live-npu-{label}",
            "evidence_strength": "single_observation",
            "title": f"Live {label} NPU runtime probe receipt",
            "conditions": {"cann": cann_version, "architecture": arch},
            "inherits_from": [],
            "provenance": {
                "source_type": "live_npu_probe",
                "source_locator": f"live-npu-probe:{label}",
                "source_digest": hashlib.sha256(probe_text.encode()).hexdigest(),
                "evidence_refs": [f"live-{label}-probe"],
                "supersedes": [],
            },
            "triggers": {
                "error_signatures": ["topology probe identity is missing from the live receipt"],
                "keywords": ["npu-smi", "topology", f"cann-{cann_version}", arch, label],
            },
        },
        "Read the probe receipt before reusing a topology-dependent rule.",
        f"vllm-ascend/runtime/live-npu-{label}/{ident}.md",
    )
    store.graded.install([live_unit])
    observed = {"cann": cann_version, "architecture": arch}
    found = store.graded.query("topology probe", conditions=observed)
    assert [item["id"] for item in found["results"]] == [live_unit.id]
    assert found["results"][0]["ready_to_apply"] is True
    ref = found["results"][0]["ref"]
    assert store.graded.explain(ref)["content"] == live_unit.body
    record_usage_event(store, root_hash=f"live-{label}", ref=ref,
                       event_type="applied", run_id=f"live-{label}")
    record_usage_event(store, root_hash=f"live-{label}", ref=ref,
                       event_type="evaluated", run_id=f"live-{label}",
                       outcome="supported", evidence_source="run_manifest_verified")
    summary = summarize_usage_events(store, live_unit.id, 1)
    assert summary["applied"] == 1 and summary["supported"] == 1
    return {"id": live_unit.id, "cann": cann_version, "architecture": arch}


def test_live_a2_and_a3_npu_capture_redaction_store_and_reme_recall(tmp_path):
    targets = _live_targets()
    a2_host, a2_container = targets["a2"]
    a3_host, a3_container = targets["a3"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        probes = {
            name: pool.submit(_ssh_probe, host, container)
            for name, (host, container) in targets.items()
        }
        outputs = {name: probe.result() for name, probe in probes.items()}
    a2_output, a3_output = outputs["a2"], outputs["a3"]

    assert "Total Count" in a2_output and "Chip Count" in a2_output
    assert "Total Count" in a3_output and "Chip Count" in a3_output

    scanner = install_scanner()
    settings = tmp_path / "community.json"
    write_settings(settings, consent_choice="contribute", roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path))

    transcript = tmp_path / "live-npu-session.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "manual-A"}}) + "\n",
        encoding="utf-8",
    )

    private_host_dir = "/home/ascend_engineer/private_bench_workspace"
    user_prompt = (
        f"Investigating NPU 显存分配 failure on A2 node {a2_host} ({a2_container}) "
        f"and A3 node {a3_host} ({a3_container}) mounted from {private_host_dir}/logs.\n"
        f"A2 probe:\n{a2_output}\n"
        f"A3 probe:\n{a3_output}\n"
    )
    assistant_reply = (
        f"Observed ACL_ERROR_RT_MEMORY_ALLOCATION during GroupedMatmulSwiglu and "
        f"torch_npu.npu_rms_norm dispatch on {a2_host} (8x1 Ascend910B4) and "
        f"{a3_host} (8x2 Ascend910C dual-die) under cann-9.1.0 at {private_host_dir}/run.py. "
        f"Setting PYTORCH_NPU_ALLOC_CONF=expandable_segments:True resolved the 显存分配 fragmentation."
    )
    _append_turn(transcript, "user", user_prompt)
    _append_turn(transcript, "assistant", assistant_reply)

    with closing(Store(tmp_path / "store", "vllm-ascend")) as store:
        engine = Engine(
            store,
            settings_path=settings,
            admission=admission,
            transcript_adapter=load_parser("codex"),
            capture_mode="public-transcript",
            redactor_executable=scanner,
        )
        captured = engine.capture(
            session_id="manual-A",
            turn_id="live-npu-turn-1",
            transcript_path=str(transcript),
        )
        engine._process(captured["id"])
        row = store.capture_row(captured["id"])
        assert row["status"] == "organized", row

        drafts = store.drafts_changed()
        assert len(drafts) == 1
        entry_id = drafts[0]["entry_id"]
        exported = store.materials.export_task(entry_id)
        full_body = package_body(exported)

        # 1. Deterministic redaction of live cluster IPs and private user paths
        assert a2_host not in full_body
        assert a3_host not in full_body
        assert private_host_dir not in full_body
        assert "ascend_engineer" not in full_body
        assert "<redacted:ipv4-address:" in full_body
        assert "<redacted:user-path:" in full_body or "<redacted:local-path:" in full_body

        # 2. Real NPU hardware topology & technical identifiers survive intact
        for preserved in (
            a2_container,
            a3_container,
            "ACL_ERROR_RT_MEMORY_ALLOCATION",
            "GroupedMatmulSwiglu",
            "torch_npu.npu_rms_norm",
            "Ascend910B4",
            "Ascend910C",
            "cann-9.1.0",
            "显存分配",
        ):
            assert preserved in full_body, preserved

        # 3. MaterialStore chunked package persistence
        assert "index.md" in exported["files"]
        assert any(name.startswith("blocks/") and name.endswith(".md") for name in exported["files"])

        # 4. ReMeIndex (TechnicalTokenizerV1) BM25 recall on NPU operators, error codes, versions, and CJK
        for query_term in (
            "ACL_ERROR_RT_MEMORY_ALLOCATION",
            "GroupedMatmulSwiglu",
            "torch_npu.npu_rms_norm",
            "npu_rms_norm",
            "cann-9.1.0",
            "显存分配",
        ):
            res = store.query(query_term)
            assert res["retrieval"] == "reme-bm25"
            assert res["results"], f"Expected recall for {query_term}"
            assert res["results"][0]["entry_id"] == entry_id

        # 5. Tier-2 graded knowledge loop over the observed live topology.
        # The unit is local test knowledge; it records a real probe identity and
        # makes no performance claim. Query/explain/usage stay on the real paths.
        result = _live_graded_check(store, f"{a2_output}\n{a3_output}", label="a2-a3")
        assert result is not None


def test_live_a3_graded_knowledge_loop_when_a2_is_unavailable(tmp_path):
    """Tier-2 A3 fallback: do not let one unreachable host hide the live path."""
    a3_host, a3_container = _live_targets()["a3"]
    output = _ssh_probe(a3_host, a3_container)
    assert "Total Count" in output and "Chip Count" in output
    with closing(Store(tmp_path / "store", "vllm-ascend")) as store:
        result = _live_graded_check(store, output, label="a3")
        assert result is not None
        assert result["cann"] and result["architecture"] == "aarch64"


@pytest.mark.live_npu
def test_live_s1_s2_multi_die_subcluster_topology_and_graded_routing(tmp_path):
    """Probe both A3 nodes, select a real S1 subcluster, and route knowledge.

    The test reads only idle NPU pairs and never starts a workload. It records
    a local S1 usage event bound to the observed Phy-ID/occupancy receipt; it
    does not claim a measured model or operator speedup.
    """
    a3_host, _ = _live_targets()["a3"]
    second_host = os.environ.get("MINDIE_LIVE_A3_SECOND_HOST", "").strip() or A3_SECOND_HOST
    targets = {"a3": a3_host, "a3-second": second_host}
    with ThreadPoolExecutor(max_workers=2) as pool:
        probes = {name: pool.submit(_ssh_topology_probe, host)
                  for name, host in targets.items()}
        outputs = {name: probe.result() for name, probe in probes.items()}
    topologies = {
        name: _parse_npu_topology(targets[name], outputs[name])
        for name in targets
    }

    for name, topology in topologies.items():
        assert topology.total_npus == 8, (name, topology.chip_counts)
        assert topology.chip_counts == {npu: 2 for npu in range(8)}
        assert topology.total_dies == 16
        assert sorted(phy for values in topology.phy_ids.values() for phy in values) == list(range(16))
        assert topology.idle_npus.isdisjoint(topology.occupied_npus)
        assert topology.idle_npus | topology.occupied_npus == frozenset(range(8))
        assert len(topology.links) == 16 * 15
        for npu, phys in topology.phy_ids.items():
            assert topology.links[(phys[0], phys[1])] == "SIO", (name, npu)
        assert topology.links[(topology.phy_ids[0][0], topology.phy_ids[1][0])] == "HCCS_SW"

    second = topologies["a3-second"]
    process_names = {name for names in second.processes.values() for name in names}
    if second.occupied_npus and any(name.startswith("VLLM") for name in process_names):
        # A partially occupied .156 must expose the vLLM worker (TP or DP) alongside the
        # engine coordinator; otherwise the idle subset is not trustworthy.
        assert any(name.startswith("VLLMWorker_") for name in process_names)

    candidates = []
    for name in ("a3-second", "a3"):
        topology = topologies[name]
        for npu_a, npu_b, phys in topology.idle_pairs():
            candidates.append((name, topology, (npu_a, npu_b, phys)))
    if not candidates:
        pytest.skip("no idle A3 dual-Die NPU pair is available for a non-destructive S1 subcluster")
    label, s1_topology, (npu_a, npu_b, phy_ids) = candidates[0]
    left, right = s1_topology.phy_ids[npu_a], s1_topology.phy_ids[npu_b]
    assert len(left) == len(right) == 2
    assert s1_topology.links[(left[0], left[1])] == "SIO"
    assert s1_topology.links[(right[0], right[1])] == "SIO"
    assert all(s1_topology.links[(first, other)] == "HCCS_SW"
               for first in left for other in right)

    s2_phy_ids = sorted(phy for values in topologies["a3"].phy_ids.values() for phy in values)
    assert s2_phy_ids == list(range(16))

    def live_conditions(*, npu_count, die_count, parallel_config, physical_ids,
                        tier, active_batch, idle_state, workload_phase=None,
                        placement="intra_node_hccs"):
        # npu-smi names the chip Ascend910 and exposes two Dies per NPU; the
        # corpus family for these A3 910C nodes is Ascend910_93. The parallel
        # config is the selected S1/S2 placement, not a measured model shape.
        conditions = {
            "npu_model": "Ascend910",
            "soc": "Ascend910_93",
            "die_topology": "same_npu_dual_die",
            "npu_count": npu_count,
            "die_count": die_count,
            "parallel_config": parallel_config,
            "placement": placement,
            "collective_backend": "hccs",
            "visible_phy_ids": list(physical_ids),
            "active_batch": active_batch,
            "idle_rank_state": idle_state,
            "dp": 2,
            "model_family": "qwen3_5",
            "repository": "vllm-ascend",
            "graph_capture_shape": "dynamic",
            "dtype": "bfloat16",
            "op_family": "reduction",
            "reduction_axis": "last",
            "tail_alignment": "32B",
            "register_pressure": "headroom",
            "output_dtype": "bfloat16",
            "workload": "fixed_shape",
            "topology_tier": tier,
        }
        if workload_phase:
            conditions["workload_phase"] = workload_phase
        return conditions

    s1_conditions = live_conditions(
        npu_count=2, die_count=4, parallel_config="TP=4", physical_ids=phy_ids,
        tier="S1", active_batch=0, idle_state="no_scheduled_requests",
    )
    s2_conditions = live_conditions(
        npu_count=8, die_count=16, parallel_config="TP4xDP2", physical_ids=s2_phy_ids,
        tier="S2", active_batch=8, idle_state="active_requests",
        workload_phase="prefill_decode_mixed", placement="disaggregated_pd_pair",
    )

    vllm_index = _live_corpus("vllm-ascend")
    operators_index = _live_corpus("operators")
    gdn_id = "unit-vllm-ascend-gdn-idle-dp-zero-pad-workspace-mutator"
    gdn_query = "GDN idle DP zero-pad workspace HCCL"
    gdn_result = vllm_index.query(gdn_query, conditions=s1_conditions, token_budget=16000)
    gdn = next((row for row in gdn_result["results"] if row["id"] == gdn_id), None)
    assert gdn is not None, sorted(row["id"] for row in gdn_result["results"])
    assert gdn["match_status"] == "matched"

    activated = {**s1_conditions, "active_batch": 1}
    activated_result = vllm_index.query(gdn_query, conditions=activated, token_budget=16000)
    assert gdn_id not in {row["id"] for row in activated_result["results"]}
    assert activated_result["excluded_conditions"].get("mismatch", 0) >= 1

    missing_batch = {key: value for key, value in s1_conditions.items() if key != "active_batch"}
    unknown_result = vllm_index.query(gdn_query, conditions=missing_batch,
                                      include_unknown=True, token_budget=16000)
    unknown_gdn = next((row for row in unknown_result["results"] if row["id"] == gdn_id), None)
    assert unknown_gdn is not None
    assert unknown_gdn["match_status"] == "condition_unknown"
    assert unknown_gdn["ready_to_apply"] is False

    prefill_id = "unit-vllm-ascend-prefill-decode-bottleneck-convergence-workflow"
    prefill_result = vllm_index.query("prefill decode bottleneck convergence",
                                      conditions=s2_conditions, token_budget=16000)
    assert prefill_id in {row["id"] for row in prefill_result["results"]}

    operator_mutator_id = "unit-vectorized-bf16-fp32-accumulation-cast-fusion-mutator"
    for tier, conditions in (("S1", s1_conditions), ("S2", s2_conditions)):
        found = operators_index.query(
            "dual Die Phy-ID memory alignment tiling reduction",
            conditions=conditions, limit=10, token_budget=16000,
        )
        ids = {row["id"] for row in found["results"]} | {
            card["id"] for card in found.get("foundation_cards", ())
        }
        assert "unit-ascend-910b-a3-memory-alignment-and-tiling-axioms" in ids, tier
        operator = next(row for row in found["results"] if row["id"] == operator_mutator_id)
        assert operator["match_status"] == "matched", tier
        assert operator["ready_to_apply"] is True, tier

    probe_text = "\n".join(outputs[name] for name in targets)
    source_digest = hashlib.sha256(probe_text.encode()).hexdigest()
    live_unit = GradedUnit.from_parts(
        {
            "schema": "mindie-graded-unit/1",
            "id": "unit-live-s1-multi-die-subcluster-topology",
            "revision": 1,
            "tier": "L4",
            "category": "mutator",
            "domain_path": "vllm-ascend/runtime/live-s1-multi-die",
            "evidence_strength": "hypothesis",
            "title": "S1 same-NPU dual-Die idle subcluster HCCS placement",
            "conditions": {
                key: s1_conditions[key] for key in (
                    "npu_model", "soc", "die_topology", "npu_count", "die_count",
                    "parallel_config", "placement", "collective_backend",
                    "visible_phy_ids", "active_batch", "idle_rank_state", "topology_tier",
                )
            },
            "inherits_from": [],
            "provenance": {
                "source_type": "live_npu_probe",
                "source_locator": f"live-npu-probe:{label}:{s1_topology.host}",
                "source_digest": source_digest,
                "evidence_refs": [f"live-{label}-npu-topology"],
                "supersedes": [],
            },
            "triggers": {
                "error_signatures": [
                    "an S1 subcluster mixes a same-NPU dual-Die pair with a cross-package Die"
                ],
                "keywords": ["S1", "same_npu_dual_die", "Phy-ID", "active_batch",
                             "HCCS", "s1multidieprobe"],
            },
        },
        "Run the idle DP rank on the paired Phy-IDs, keep the zero-padded workspace, "
        "and join the HCCL collective; fall back when a Die crosses packages or "
        "active_batch is nonzero. s1multidieprobe",
        "vllm-ascend/runtime/live-s1-multi-die/unit-live-s1-multi-die-subcluster-topology.md",
    )
    with closing(Store(tmp_path / "store", "vllm-ascend")) as store:
        installed = store.graded.install([live_unit])
        assert installed["units"] == 1
        found = store.graded.query("s1multidieprobe", conditions=s1_conditions, token_budget=8000)
        row = next(row for row in found["results"] if row["id"] == live_unit.id)
        assert row["match_status"] == "matched"
        assert row["ready_to_apply"] is True
        assert row["missing_conditions"] == []
        assert store.graded.explain(row["ref"])["content"] == live_unit.body

        for misconfigured in (
            {**s1_conditions, "die_topology": "cross_package"},
            {**s1_conditions, "die_topology": "single_die", "npu_count": 1, "die_count": 1,
             "visible_phy_ids": [phy_ids[0]], "parallel_config": "TP=1", "active_batch": 1},
            {**s1_conditions, "active_batch": 1},
        ):
            rejected = store.graded.query("s1multidieprobe", conditions=misconfigured)
            assert rejected["results"] == []
            assert rejected["excluded_conditions"].get("mismatch", 0) == 1

        run_id = f"s1-{label}-{npu_a}-{npu_b}"
        record_usage_event(store, root_hash=source_digest, ref=row["ref"],
                           event_type="applied", run_id=run_id)
        record_usage_event(
            store, root_hash=source_digest, ref=row["ref"],
            event_type="evaluated", run_id=run_id, outcome="supported",
            evidence_source="run_manifest_verified",
            reason=f"live {label} topology receipt: Phy-ID {','.join(map(str, phy_ids))}",
        )
        summary = summarize_usage_events(store, live_unit.id, 1, include_delivery=True)
        assert summary["applied"] == 1
        assert summary["evaluated"] == 1
        assert summary["supported"] == 1
        assert summary["evidence_sources"]["run_manifest_verified"] == 1
        assert summary["query_deliveries"] >= 1
