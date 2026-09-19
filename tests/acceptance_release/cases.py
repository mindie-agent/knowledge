"""Fixed case inventory from repo-public artifacts and isolated fixtures.

These are evaluation *tasks*, not expected answers for a model. Windows, NPU,
and model outcomes stay unknown until Codex records a matching real run.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

REQUIRED_MODEL = "gpt-5.6-luna"
REQUIRED_REASONING = "max"

# Per-case wall/call caps. Zero auto-retry is global.
DEFAULT_WALL_SECONDS = 900
DEFAULT_MAX_CALLS = 8

CASES: list[dict] = [
    {
        "id": "migration-mcp-stdio",
        "family": "corpus_migration",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_applicable",
        "source": "corpus/references/mcp-stdio-newline-delimited-jsonrpc.md",
        "question": "Does the public MCP stdio note map to knowledge with url, revision, and conditions?",
        "labels": ["mappable_knowledge"],
        "notes": "Repo-public specification excerpt. Not a local runtime measurement.",
    },
    {
        "id": "migration-910b4-peaks",
        "family": "corpus_migration",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_executed_this_round",
        "source": "corpus/references/ascend910b4-cann-9.0.0-platform-config-peaks.md",
        "question": "Are theoretical CANN 9.0.0 / Ascend910B4 peaks retained as conditioned knowledge, not as measured throughput?",
        "labels": ["version_conditioned", "theoretical_not_measured"],
        "notes": "Packaged corpus. Theoretical declaration; no device is opened.",
    },
    {
        "id": "migration-conflict-versions",
        "family": "corpus_migration",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_applicable",
        "source": "tests/acceptance_release/fixtures/notes/",
        "question": "Do two notes with the same title and incompatible CANN conditions both remain?",
        "labels": ["conflict_retain"],
        "notes": "Isolated fixtures, not production feed.",
    },
    {
        "id": "migration-unmappable-and-private",
        "family": "corpus_migration",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_applicable",
        "source": "tests/acceptance_release/fixtures/notes/",
        "question": "Are notes without source/conditions reported unmappable, and private sidecars skipped unpublished?",
        "labels": ["unmappable", "skip_private"],
        "notes": "Isolated fixtures. Private summaries must not be uploaded.",
    },
    {
        "id": "migration-feed-layout",
        "family": "corpus_migration",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_applicable",
        "source": "tests/acceptance_release/fixtures/feed/",
        "question": "Do topics/ become knowledge, cases/ experience, and maintenance/ skipped?",
        "labels": ["feed_topics_cases", "skip_maintenance"],
        "notes": "Isolated feed-shaped export. Official origin/knowledge/vllm-ascend objects were not in this clone.",
    },
    {
        "id": "no-hit",
        "family": "retrieval_negative",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_applicable",
        "source": "corpus/references/mcp-stdio-newline-delimited-jsonrpc.md",
        "question": "Query nonexistent_operator_zz991 must not invent a hit.",
        "labels": ["no_hit"],
        "notes": "Use the actual imported public corpus. This is a retrieval contract, not NPU evidence.",
    },
    {
        "id": "version-inapplicable",
        "family": "retrieval_negative",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_executed_this_round",
        "source": "corpus/references/ascend910b4-cann-9.0.0-platform-config-peaks.md",
        "question": "A CANN=8 query must not return CANN 9.0.0-only knowledge as applicable.",
        "labels": ["version_inapplicable"],
        "notes": "Applicability filter on knowledge; the old note is not thereby false.",
    },
    {
        "id": "hit-unused",
        "family": "use_attribution",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_applicable",
        "source": "corpus/references/mcp-stdio-newline-delimited-jsonrpc.md",
        "question": "A lexical hit that the task never applied is not a use and not helpful.",
        "labels": ["hit_unused"],
        "notes": "Query may hit the peak table while the task is about MCP stdio.",
    },
    {
        "id": "consumption-echo",
        "family": "use_attribution",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_applicable",
        "source": "tests/test_mindie_loop.py",
        "question": "Re-adding consumed experience text must not turn the consumer into a producer or extra vote.",
        "labels": ["consumption_echo"],
        "notes": "Local Store identity behavior from the current loop tests; not a model judgment.",
    },
    {
        "id": "success-no-contribution",
        "family": "effect_evidence",
        "requires_model": True,
        "windows": "historical_record_not_reverified",
        "npu": "not_executed_this_round",
        "source": "tools/knowledge-intake/ACCEPTANCE-2026-09-13.md",
        "question": "A later task can succeed while the retrieved experience contributed nothing.",
        "labels": ["success_no_contribution"],
        "notes": (
            "Historical intake acceptance is a real dated Windows/tool record. "
            "It is not vLLM-Ascend NPU evidence and not this round's model run. "
            "Codex must execute the effect case with gpt-5.6-luna / max; G1 does not."
        ),
    },
    {
        "id": "failure-hypothesis-excluded",
        "family": "effect_evidence",
        "requires_model": True,
        "windows": "not_verified",
        "npu": "not_executed_this_round",
        "source": "corpus/references/mcp-stdio-newline-delimited-jsonrpc.md",
        "question": "Can a failed task use protocol evidence to correctly exclude transport as the failure cause without being judged unhelpful merely because the task failed?",
        "labels": ["failure_hypothesis_excluded"],
        "notes": "The public specification supplies context only. The executor must provide actual task commands and artifacts showing the excluded hypothesis; the note itself proves no effect.",
    },
    {
        "id": "abc-protocol",
        "family": "effect_evidence",
        "requires_model": False,
        "windows": "not_verified",
        "npu": "not_executed_this_round",
        "source": "tests/test_mindie_loop.py",
        "question": "A→B use → independent judge → C retrieval can be recorded without treating helpful or score change as effect success.",
        "labels": ["abc_protocol", "unknown"],
        "notes": "Local ledger protocol. Effect on a real task is unknown until Codex runs abc-effect.",
    },
    {
        "id": "abc-effect",
        "family": "effect_evidence",
        "requires_model": True,
        "windows": "not_verified",
        "npu": "not_executed_this_round",
        "source": "tools/knowledge-intake/ACCEPTANCE-2026-09-13.md",
        "question": "Independent task C, with frozen corpus, shows reduced investigation or avoided error using original artifacts — or unknown.",
        "labels": ["unknown"],
        "notes": (
            "Codex-only model execution. Plan/start/complete/valid are separate. "
            "Do not auto-retry. Do not score success from helpful or DB weight."
        ),
    },
    {
        "id": "negative-repeat-correction",
        "family": "effect_evidence",
        "requires_model": True,
        "windows": "not_verified",
        "npu": "not_executed_this_round",
        "source": "tests/test_mindie_loop.py",
        "question": "Unknown stays neutral; repeated independent unhelpful can withdraw search without deleting content; a later correction is a new use.",
        "labels": ["unknown", "negative", "repeat_correction"],
        "notes": "Model judge must be independent of producer and consumer. G1 does not call it.",
    },
]


def case_by_id(ident: str) -> dict:
    for item in CASES:
        if item["id"] == ident:
            return dict(item)
    raise KeyError(f"unknown acceptance case: {ident}")


def public_source(item: dict) -> Path:
    return REPO_ROOT / item["source"]
