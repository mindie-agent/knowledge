"""Domain-specific retrieval accuracy (ReMe BM25 + TechnicalTokenizerV1) and
multi-turn incremental transcript capture, redaction, and summary quality tests.
"""
from __future__ import annotations

import json
import time
from contextlib import closing
from datetime import datetime, timezone

import pytest

from conftest import make_admission, write_settings
from mindie_knowledge.loop import transcript_capture
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.export import build_batch
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_redaction import install_scanner
from mindie_knowledge.materials.publication import load_batch_payload
from mindie_knowledge.materials.store import validate_package_files
from lane_support import load_parser
from material_worker_fixture import command as summary_command, package_body


@pytest.fixture(scope="module")
def scanner():
    return install_scanner()


def _domain_id(index: int) -> str:
    return f"{index:064x}"


ASCEND_CORPUS = [
    dict(
        entry_id=_domain_id(1),
        kind="experience",
        title="Resolve ACL_ERROR_RT_MEMORY_ALLOCATION (507001) HBM fragmentation during long-context prefill",
        summary=(
            "Mitigate NPU HBM allocator fragmentation on Ascend 910B/910C by tuning "
            "PYTORCH_NPU_ALLOC_CONF=max_split_size_mb:256 and lowering gpu_memory_utilization."
        ),
        conditions={"chip": "910b", "component": "runtime-memory"},
        content=(
            "## Symptom\n"
            "During 32k prefill on Ascend 910B, vLLM-Ascend aborts with:\n"
            "`RuntimeError: ACL_ERROR_RT_MEMORY_ALLOCATION (error code 507001)` even though "
            "`npu-smi info` reports 6.5 GB of free HBM.\n\n"
            "## Root Cause\n"
            "Variable-length prompt batches fragment the CachingAllocator block pool when "
            "large contiguous workspace tensors (>512 MB) are requested after KV cache reservation.\n\n"
            "## Resolution\n"
            "Set `export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True,max_split_size_mb:256` "
            "and cap `--gpu-memory-utilization 0.90` before launching the MindIE or vLLM worker.\n"
        ),
    ),
    dict(
        entry_id=_domain_id(2),
        kind="experience",
        title="Ascend A3 910C dual-Die Phy-ID mapping and ASCEND_RT_VISIBLE_DEVICES rank binding",
        summary=(
            "Map logical NPU indices to dual-Die physical IDs (Phy-ID 0..15) on Atlas 800I A3 "
            "to prevent cross-package HCCL ring initialization failures."
        ),
        conditions={"chip": "910c", "component": "topology"},
        content=(
            "## Symptom\n"
            "On Atlas A3 (Ascend 910C dual-Die per package), passing `ASCEND_RT_VISIBLE_DEVICES=0,1,2,3` "
            "with `npu-smi info -m` showing `NPU ID 0 -> Chip ID 0,1 (Phy-ID 0,1)` fails with "
            "`ACL_ERROR_RT_INVALID_DEVICEID` when mixing NPU package IDs and Die Phy-IDs.\n\n"
            "## Root Cause\n"
            "Each Ascend 910C physical card exposes two logical dies (`Phy-ID 2*k` and `2*k+1`). "
            "`ASCEND_RT_VISIBLE_DEVICES` expects contiguous zero-based logical die indices (0..15).\n\n"
            "## Resolution\n"
            "Always specify paired die `Phy-ID` values (for example `ASCEND_RT_VISIBLE_DEVICES=14,15` "
            "for physical NPU 7 Chips 0 and 1) verified via `npu-smi info -m`.\n"
        ),
    ),
    dict(
        entry_id=_domain_id(3),
        kind="experience",
        title="Diagnose HCCL_E_WAIT_TIMEOUT (EI0006) in multi-node RoCEv2 tensor-parallel inference",
        summary=(
            "Fix multi-node HCCL AllReduce wait timeouts by aligning HCCL_IF_IP, "
            "HCCL_SOCKET_IFNAME, and checking RoCEv2 link state via hccn_tool."
        ),
        conditions={"chip": "910c", "component": "hccl"},
        content=(
            "## Symptom\n"
            "Multi-node TP16 inference stalls for 120s and raises `HCCL_E_WAIT_TIMEOUT` (`EI0006`) "
            "during `HcclAllReduce` p2p socket handshake.\n\n"
            "## Root Cause\n"
            "Host control plane selected a Docker bridge subnet instead of the bond0 host NIC, "
            "or a RoCEv2 optical transceiver link dropped (`hccn_tool -i 0 -link -g` shows `DOWN`).\n\n"
            "## Resolution\n"
            "1. Export `HCCL_IF_IP=<host-bond0-ip>` and `HCCL_SOCKET_IFNAME=bond0`.\n"
            "2. Increase `HCCL_CONNECT_TIMEOUT=1800` and `HCCL_EXEC_TIMEOUT=1800` for large MoE weights.\n"
            "3. Verify all 16 RoCE ports with `for i in $(seq 0 7); do hccn_tool -i $i -link -g; done`.\n"
        ),
    ),
    dict(
        entry_id=_domain_id(4),
        kind="experience",
        title="Fix aclnnGroupedMatmulSwiglu ACL_ERROR_INVALID_PARAM (161002) MoE token alignment",
        summary=(
            "Pad expert group_list cumulative token counts and hidden dimensions to 16-element "
            "fractal boundaries required by aclnnGroupedMatmulSwiglu."
        ),
        conditions={"chip": "910b", "component": "operator"},
        content=(
            "## Symptom\n"
            "Fused MoE kernel `aclnnGroupedMatmulSwiglu` (`GroupedMatmulSwiglu`) fails during "
            "decode with `ACL_ERROR_INVALID_PARAM (161002): group_list tensor dtype or alignment invalid`.\n\n"
            "## Root Cause\n"
            "The CANN `aclnnGroupedMatmulSwiglu` operator requires `group_list` in `int64` cumulative "
            "sum format and inner dimension `K` aligned to 32 bytes (16 elements in `bfloat16`).\n\n"
            "## Resolution\n"
            "Convert `expert_tokens` via `torch.cumsum(expert_tokens, dim=0, dtype=torch.int64)` "
            "and pad unaligned expert hidden slices before invoking `torch_npu.npu_grouped_matmul`.\n"
        ),
    ),
    dict(
        entry_id=_domain_id(5),
        kind="experience",
        title="DeepSeek-V3 Multi-Token Prediction (MTP) speculative decoding NaN logits under bfloat16 RoPE",
        summary=(
            "Prevent NaN logits and token acceptance collapse in DeepSeek-V3 MTP eagle/draft head "
            "by masking unallocated paged KV cache slots and casting yarn RoPE cos/sin to float32."
        ),
        conditions={"chip": "910c", "component": "accuracy"},
        content=(
            "## Symptom\n"
            "When running DeepSeek-V3 with `--num-speculative-tokens 2` (MTP speculative decoding), "
            "draft token acceptance rate drops to 0% after 4096 tokens and `lm_head` produces `NaN`.\n\n"
            "## Root Cause\n"
            "YaRN `bfloat16` rotary embedding (`npu_rotary_mul`) accumulates phase rounding error at "
            "positions > 4096, and rejected speculative slots leave stale keys in the PagedAttention block.\n\n"
            "## Resolution\n"
            "Keep `cos_sin_cache` in `torch.float32` until RoPE application and zero out rejected "
            "`slot_mapping` entries (`-1` pad mask) before the next MTP draft step.\n"
        ),
    ),
    dict(
        entry_id=_domain_id(6),
        kind="experience",
        title="Container startup ACL_ERROR_UNINITIALIZE (100001) when /dev/davinci_manager or driver lib64 is missing",
        summary=(
            "Mount /dev/davinci_manager, /dev/devmm_svm, /dev/hisi_hdc, and /usr/local/Ascend/driver "
            "into the inference container so libascendcl.so initializes cleanly."
        ),
        conditions={"chip": "910b", "component": "container"},
        content=(
            "## Symptom\n"
            "Starting Python inside a MindIE Docker container fails immediately on `import torch_npu` "
            "with `ACL_ERROR_UNINITIALIZE (100001)` or `libascend_hal.so: cannot open shared object file`.\n\n"
            "## Root Cause\n"
            "The container was started with `--device=/dev/davinci0` but omitted the host management "
            "character devices (`/dev/davinci_manager`, `/dev/devmm_svm`, `/dev/hisi_hdc`) or "
            "`-v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro`.\n\n"
            "## Resolution\n"
            "Pass all three management devices plus `/usr/local/Ascend/driver` and `/usr/local/sbin/npu-smi` "
            "when running the container.\n"
        ),
    ),
]


def test_ascend_domain_corpus_top1_top3_recall_and_no_crosstalk(tmp_path):
    """Verify TechnicalTokenizerV1 + ReMe BM25 achieves 100% Top-1 recall across
    Ascend/MindIE error codes, operators, and mixed natural-language queries.
    """
    with closing(Store(tmp_path / "store", "ascend")) as store:
        for doc in ASCEND_CORPUS:
            store.create_draft(**doc)

        queries_and_expected_top1 = [
            # 1. Error code + HBM allocator tuning env var
            ("ACL_ERROR_RT_MEMORY_ALLOCATION 507001 PYTORCH_NPU_ALLOC_CONF max_split_size_mb", _domain_id(1)),
            # 2. A3 910C dual-Die Phy-ID mapping
            ("Atlas A3 910C dual-Die Phy-ID 14,15 ASCEND_RT_VISIBLE_DEVICES npu-smi info -m", _domain_id(2)),
            # 3. Multi-node HCCL timeout + RoCE link tool
            ("HCCL_E_WAIT_TIMEOUT EI0006 HCCL_IF_IP hccn_tool RoCEv2 AllReduce timeout", _domain_id(3)),
            # 4. Fused MoE operator shape alignment
            ("aclnnGroupedMatmulSwiglu ACL_ERROR_INVALID_PARAM 161002 group_list int64 alignment", _domain_id(4)),
            # 5. DeepSeek-V3 MTP speculative decoding precision
            ("DeepSeek-V3 MTP num-speculative-tokens NaN logits npu_rotary_mul slot_mapping", _domain_id(5)),
            # 6. Docker container device mount missing
            ("Docker container ACL_ERROR_UNINITIALIZE 100001 /dev/davinci_manager /dev/hisi_hdc libascend_hal.so", _domain_id(6)),
        ]

        for query_text, expected_id in queries_and_expected_top1:
            response = store.query(query_text, limit=3)
            results = response["results"]
            assert results, f"Expected non-empty results for query: {query_text}"
            top1_id = results[0]["entry_id"]
            top3_ids = [item["entry_id"] for item in results[:3]]
            assert top1_id == expected_id, (
                f"Top-1 mismatch for query {query_text!r}: got {top1_id}, expected {expected_id}"
            )
            assert expected_id in top3_ids
            # Verify the top match reference resolves cleanly through store.explain()
            block_view = store.explain(results[0]["ref"])
            assert block_view["kind"] == "block"
            assert block_view["entry_id"] == expected_id
            assert len(block_view["content"]) > 40
            task_view = store.explain(block_view["task_ref"])
            assert task_view["kind"] == "task"
            assert task_view["first_block_ref"] == results[0]["ref"]

        # Verify condition matching boosts 910c entries above 910b entries on shared terms
        filtered_910c = store.query(
            "Ascend NPU troubleshooting symptom root cause resolution",
            limit=3,
            conditions={"chip": "910c", "component": "topology"},
        )
        assert filtered_910c["results"][0]["entry_id"] == _domain_id(2)

        # Verify natural Chinese-English bilingual queries achieve 100% Top-1 recall
        bilingual_queries_and_expected_top1 = [
            ("长序列预填充时报显存碎片和内存分配失败应该怎么调优", _domain_id(1)),
            ("Atlas A3 双Die 物理ID映射 绑卡报错怎么排查", _domain_id(2)),
            ("多机推理 集合通信超时 网卡绑定和链路状态怎么查", _domain_id(3)),
            ("MoE 分组矩阵乘 激活融合 参数非法报错怎么解决", _domain_id(4)),
            ("DeepSeek-V3 投机采样 草稿头 旋转位置编码 精度异常怎么修复", _domain_id(5)),
            ("容器启动报设备挂载或驱动缺失错误怎么处理", _domain_id(6)),
        ]
        for query_text, expected_id in bilingual_queries_and_expected_top1:
            response = store.query(query_text, limit=3)
            results = response["results"]
            assert results, f"Expected non-empty results for bilingual query: {query_text}"
            assert results[0]["entry_id"] == expected_id, (
                f"Bilingual Top-1 mismatch for {query_text!r}: got {results[0]['entry_id']}, expected {expected_id}"
            )


def test_citation_graph_groups_related_troubleshooting_blocks(tmp_path):
    """Verify StrictFileGraph groups a follow-up mitigation entry citing a root-cause entry."""
    with closing(Store(tmp_path / "store", "ascend")) as store:
        root_doc = store.create_draft(**ASCEND_CORPUS[2])  # HCCL_E_WAIT_TIMEOUT entry_id=3
        followup_id = _domain_id(7)
        store.create_draft(
            entry_id=followup_id,
            kind="experience",
            title="Canary benchmark verification for HCCL_IF_IP bond0 fix on 2-node A3 cluster",
            summary="Confirmed TP16 DeepSeek-R1 benchmark completes after applying bond0 HCCL_IF_IP.",
            conditions={"chip": "910c", "component": "hccl"},
            content=(
                f"Applied the RoCEv2 interface binding fix documented in mindie://ascend/{root_doc['entry_id']} "
                "and verified `HCCL_E_WAIT_TIMEOUT` no longer occurs across 500 prefill/decode iterations.\n"
            ),
        )
        response = store.query("HCCL_E_WAIT_TIMEOUT HCCL_IF_IP bond0 RoCEv2", limit=5)
        results = response["results"]
        assert results
        # Both the root cause and the citing follow-up are grouped into a single citation group
        top_group = results[0]
        assert top_group["group_basis"] == "citation"
        grouped_ids = {top_group["entry_id"]} | {r["entry_id"] for r in top_group.get("related", [])}
        assert grouped_ids == {root_doc["entry_id"], followup_id}


def test_multiturn_incremental_transcript_capture_redaction_and_summary_fidelity(tmp_path, scanner):
    """Verify a multi-turn Ascend debugging transcript is captured incrementally,
    redacts all private IPs/paths/credentials, preserves all technical error codes
    and operator names, and produces valid indexed mindie-entry/3 + mindie-material-block/1 packages.
    """
    settings = tmp_path / "community.json"
    write_settings(settings, consent_choice="contribute", roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path))
    transcript_path = tmp_path / "native.jsonl"
    transcript_path.write_text(
        json.dumps(dict(type="session_meta", payload=dict(id="manual-A"))) + "\n",
        encoding="utf-8",
    )

    def append_turn(role: str, text: str) -> None:
        record = dict(
            type="response_item",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=dict(
                type="message",
                role=role,
                content=[dict(type="input_text" if role == "user" else "output_text", text=text)],
            ),
        )
        with transcript_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    with closing(Store(tmp_path / "store", "ascend")) as store:
        engine = Engine(
            store,
            settings_path=settings,
            admission=admission,
            transcript_adapter=load_parser("codex"),
            capture_mode="public-transcript",
            redactor_executable=scanner,
            summary_command=summary_command(
                title="Resolve 910C dual-Die ACL_ERROR_RT_MEMORY_ALLOCATION and aclnnGroupedMatmulSwiglu alignment",
                summary="Tune PYTORCH_NPU_ALLOC_CONF=max_split_size_mb:256 and align MoE group_list to int64.",
            ),
        )

        # Turn 1: Initial error report containing private IP, secret token, local path, and real Ascend stack trace
        append_turn(
            "user",
            (
                "On node ssh://root@192.168.13.165:22 running `/Users/maoxx241/private_repo/serve_vllm.py` "
                "with token `ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8`, worker rank 1 crashed:\n"
                "Traceback (most recent call last):\n"
                "  File \"vllm_ascend/worker/model_runner.py\", line 412, in execute_model\n"
                "    output = torch_npu.npu_grouped_matmul(x, weight, group_list=expert_tokens)\n"
                "RuntimeError: aclnnGroupedMatmulSwiglu failed with ACL_ERROR_INVALID_PARAM (161002) "
                "followed by ACL_ERROR_RT_MEMORY_ALLOCATION (507001)."
            ),
        )
        cap1 = engine.capture(session_id="manual-A", turn_id="turn-1", transcript_path=str(transcript_path))
        engine._process(cap1["id"])
        assert store.capture_row(cap1["id"])["status"] == "organized"

        # Turn 2: Follow-up experiment on Phy-ID 14,15 with another private IP
        append_turn(
            "user",
            (
                "Tested on peer host 10.24.88.19 and 173.125.1.2 with `ASCEND_RT_VISIBLE_DEVICES=14,15` "
                "(Phy-ID 14 and 15). Converted `group_list` via `torch.cumsum(expert_tokens, dim=0, dtype=torch.int64)` "
                "and set `export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True,max_split_size_mb:256`."
            ),
        )
        cap2 = engine.capture(session_id="manual-A", turn_id="turn-2", transcript_path=str(transcript_path))
        engine._process(cap2["id"])
        assert store.capture_row(cap2["id"])["status"] == "organized"

        # Turn 3: Final verification turn
        append_turn(
            "user",
            (
                "Verified: `aclnnGroupedMatmulSwiglu` and `ACL_ERROR_RT_MEMORY_ALLOCATION` are both resolved; "
                "32k prefill throughput reached 1840 tokens/s without HBM OOM."
            ),
        )
        cap3 = engine.capture(session_id="manual-A", turn_id="turn-3", transcript_path=str(transcript_path))
        engine._process(cap3["id"])
        assert store.capture_row(cap3["id"])["status"] == "organized"

        # Run incremental summary indexing across all 3 queued batches
        for _ in range(3):
            engine.last_activity = time.monotonic() - 10
            with store._write_txn():
                store.db.execute("UPDATE transcript_tasks SET summary_due=0")
            transcript_capture.summarize_due(engine)

        # Verify draft material structure and 3 incremental blocks
        drafts = store.drafts_changed(generation=engine._settings().generation)
        assert len(drafts) == 1
        entry_id = drafts[0]["entry_id"]
        full_body = drafts[0]["content"]

        # 1. Privacy redaction check: zero leakage of private IPs, tokens, or local user paths
        for forbidden in (
            "192.168.13.165",
            "10.24.88.19",
            "173.125.1.2",
            "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
            "private_repo",
            "maoxx241",
        ):
            assert forbidden not in full_body, f"Leaked private value {forbidden!r} in captured body"

        # 2. Technical fidelity check: error codes, operator names, env vars, and traceback lines remain intact
        for preserved in (
            "aclnnGroupedMatmulSwiglu",
            "ACL_ERROR_INVALID_PARAM",
            "161002",
            "ACL_ERROR_RT_MEMORY_ALLOCATION",
            "507001",
            "line 412, in execute_model",
            "ASCEND_RT_VISIBLE_DEVICES=14,15",
            "PYTORCH_NPU_ALLOC_CONF=expandable_segments:True,max_split_size_mb:256",
            "torch.cumsum(expert_tokens, dim=0, dtype=torch.int64)",
        ):
            assert preserved in full_body, f"Lost technical entity {preserved!r} during capture/redaction"

        # 3. Verify package export & mindie-entry/3 + mindie-material-block/1 reference chain
        built = build_batch(store, settings=engine._settings())
        assert built is not None
        batch_id = built[0]
        payload = load_batch_payload(store, store.batch(batch_id))
        package = validate_package_files(
            {item["path"].split("/", 2)[2]: item["content"] for item in payload["files"]},
            domain="ascend",
        )
        assert package["ready"] is True
        assert package["entry"]["schema"] == "mindie-entry/3"
        task_manifest = store.materials.read_task(entry_id, source="draft")
        assert len(task_manifest["blocks"]) == 3
        assert all(block["indexed"] is True for block in task_manifest["blocks"])
        assert package_body(package) == full_body

        # 4. Verify retrieval and block-chain traversal via store.query() and store.explain()
        search_res = store.query("aclnnGroupedMatmulSwiglu PYTORCH_NPU_ALLOC_CONF max_split_size_mb", limit=3)
        assert search_res["results"]
        assert search_res["results"][0]["entry_id"] == entry_id

        task_info = store.explain(f"mindie://ascend/{entry_id}")
        assert task_info["kind"] == "task"
        assert task_info["block_count"] == 3
        visited_blocks = []
        cursor_ref = task_info["first_block_ref"]
        while cursor_ref is not None:
            blk = store.explain(cursor_ref)
            assert blk["kind"] == "block"
            visited_blocks.append(blk["content"])
            cursor_ref = blk["next_block_ref"]
        assert len(visited_blocks) == 3
        assert "".join(visited_blocks) == full_body
