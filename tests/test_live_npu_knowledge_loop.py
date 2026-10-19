"""Live NPU cluster (A2 & A3) end-to-end integration test for the knowledge loop.

Connects via SSH to real Ascend A2 (173.125.1.2, container mindie-bench-a2-core-20261017)
and A3 (192.168.13.165, container vaws-offload-pd165-20261016) environments, captures
real NPU hardware/CANN topology and operator troubleshooting logs, and verifies:
  TranscriptCapture -> Redaction (IPv4 / private path masking + Gitleaks) ->
  MaterialStore chunked persistence -> ReMe BM25 technical tokenizer recall.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import subprocess
import time

import pytest

from conftest import make_admission, write_settings
from lane_support import load_parser
from material_worker_fixture import package_body
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transcript_redaction import install_scanner

pytestmark = pytest.mark.live_npu


A2_HOST = "173.125.1.2"
A2_CONTAINER = "mindie-bench-a2-core-20261017"
A3_HOST = "192.168.13.165"
A3_CONTAINER = "vaws-offload-pd165-20261016"


def _ssh_probe(host: str, container: str) -> str:
    cmd = [
        "ssh",
        "-n",
        "-o", "ControlMaster=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        f"root@{host}",
        (
            f"npu-smi info -l && "
            f"docker exec {container} sh -c "
            f"'ls -d /usr/local/Ascend/* 2>/dev/null; uname -m'"
        ),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"Live NPU host {host} unreachable within 15s: {exc}")
    if res.returncode != 0:
        pytest.skip(f"Live NPU host {host} returned {res.returncode}: {res.stderr.strip()}")
    return res.stdout.strip()


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


def test_live_a2_and_a3_npu_capture_redaction_store_and_reme_recall(tmp_path):
    a2_output = _ssh_probe(A2_HOST, A2_CONTAINER)
    a3_output = _ssh_probe(A3_HOST, A3_CONTAINER)

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
        f"Investigating NPU 显存分配 failure on A2 node {A2_HOST} ({A2_CONTAINER}) "
        f"and A3 node {A3_HOST} ({A3_CONTAINER}) mounted from {private_host_dir}/logs.\n"
        f"A2 probe:\n{a2_output}\n"
        f"A3 probe:\n{a3_output}\n"
    )
    assistant_reply = (
        f"Observed ACL_ERROR_RT_MEMORY_ALLOCATION during GroupedMatmulSwiglu and "
        f"torch_npu.npu_rms_norm dispatch on {A2_HOST} (8x1 Ascend910B4) and "
        f"{A3_HOST} (8x2 Ascend910C dual-die) under cann-9.1.0 at {private_host_dir}/run.py. "
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
        assert A2_HOST not in full_body
        assert A3_HOST not in full_body
        assert private_host_dir not in full_body
        assert "ascend_engineer" not in full_body
        assert "<redacted:ipv4-address:" in full_body
        assert "<redacted:user-path:" in full_body or "<redacted:local-path:" in full_body

        # 2. Real NPU hardware topology & technical identifiers survive intact
        for preserved in (
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
