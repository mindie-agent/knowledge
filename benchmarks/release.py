"""[Role] Measure production technical tokenization on public synthetic input.
[Invariants] Qualified identifiers, versions and CJK search terms remain intact.
[Timeouts & Locks] Fixed warmup and 10,000 in-memory iterations; no locks.
[Side-Effect Policy] Writes only the requested machine-readable benchmark result.
"""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mindie_knowledge.retrieval import tokens

TEXT = (
    "Ascend 910C CANN-9.1.0 torch_npu.npu_rms_norm "
    "ACL_ERROR_RT_MEMORY_ALLOCATION PYTORCH_NPU_ALLOC_CONF "
    "\u663e\u5b58\u5206\u914d \u7b97\u5b50\u6027\u80fd "
) * 8
EXPECTED = {
    "torch_npu.npu_rms_norm", "npu_rms_norm", "cann-9.1.0",
    "acl_error_rt_memory_allocation", "\u663e\u5b58", "\u5206\u914d",
}


def main():
    for _ in range(100):
        observed = tokens(TEXT)
    assert EXPECTED <= set(observed)
    iterations = 10000
    started = time.perf_counter_ns()
    for _ in range(iterations):
        observed = tokens(TEXT)
    elapsed = time.perf_counter_ns() - started
    assert EXPECTED <= set(observed)
    result = {
        "status": "pass",
        "metrics": [{
            "name": "technical_tokenization", "unit": "us/op",
            "value": elapsed / iterations / 1000, "iterations": iterations,
        }],
    }
    Path(os.environ["MINDIE_BENCH_RESULT_PATH"]).write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
