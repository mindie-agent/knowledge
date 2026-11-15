"""Content-verified live ingestion for the four pinned reference repositories.

[Role]
Read the real `cannbot-skills`, `cannbot-dsl`, `cannbot-knowledge`, and
`vllm-ascend` files, resolve each repository's exact git commit (including
worktree/packed-refs layouts), verify operator/source/test paths and headings,
and record a SHA-256 for every ingested file.
[Invariants]
Missing repositories raise DistillError unless `allow_offline=True`; offline
ingestion is explicitly marked degraded_offline=True / live_verified=False.
TimeoutError is re-raised before any OSError handler. Counts alone never
establish completeness: exact membership and content markers are required.
[Timeouts & Locks]
Bounded reads of known files only; no waits, locks, or recursive tree walks.
[Side-Effect Policy]
Read-only scans; no files are created or modified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DEFAULT_REFS_ROOT = "/Users/maoxx241/code/refs"
DEFAULT_VLLM_ROOT = "/Users/maoxx241/code/vllm-ascend"
PINNED_COMMITS: Mapping[str, str] = {
    "cannbot-skills": "750045df251d031bf708de9dc1614c2202eed6dd",
    "cannbot-dsl": "b554a39893a4e024276214a4abaa1a9c2a26a460",
    "cannbot-knowledge": "1ccd9e604eb82c45e8a6e69d0d99d487d5ba2f84",
    "vllm-ascend": "6e3299ab492ddbadb4c8e3a00b88c982c9a28131",
}

STATIC_OPERATORS: tuple[str, ...] = (
    "fused_norm_rope_attn_rope_cast_fwd", "add", "attn_epilogue", "attn_prologue",
    "flash_kda", "flash_kda_fwd", "flash_kda_cp_preprocess", "flash_kda_cp_main",
    "matmul", "batch_matmul", "fused_matmul", "dual_level_quant_matmul",
    "quant_matmul", "grouped_matmul", "grouped_matmul_finalize_routing",
    "transpose_batch_matmul", "qwen_sparse_attn", "stem_indexer", "qsa_indexer",
    "indexer_prologue_k", "engram_gate", "quant_block_sparse_attn",
    "indexer_prologue_qw", "quant_lightning_indexer",
    "quant_lightning_indexer_metadata", "quant_sparse_lightning_indexer",
    "quant_sparse_lightning_indexer_metadata", "mixed_quant_sparse_flash_mla",
    "mixed_quant_sparse_flash_mla_metadata", "block_attn_res_update_rms_norm",
    "fused_recurrent_kda_snapshot", "fused_recurrent_kda_replayssm",
    "mega_recurrent_kda", "commit_recurrent_kda", "quant_mla_prolog",
    "mla_prolog_v4", "gather_v2", "scatter_elements", "moonep", "sparse_flash_mla",
    "sparse_flash_mla_metadata", "dynamic_mx_quant_sr", "mega_full_index",
    "mega_reindex", "matmul_streamk", "quant_batch_matmul_mxa8w4", "mega_mla",
    "quant_batch_matmul_inplace_add", "weight_quant_batchmatmul", "flash_attn",
    "flash_mla_with_kvcache", "flash_attn_fp8_fullquant", "mega_compressor",
)
STATIC_K3: tuple[str, ...] = (
    "block_attn_res_update_rms_norm", "flash_kda", "flash_kda_fwd",
    "flash_kda_cp_preprocess", "flash_kda_cp_main", "fused_recurrent_kda_snapshot",
    "fused_recurrent_kda_replayssm", "mega_recurrent_kda", "commit_recurrent_kda",
    "quant_mla_prolog",
)
STATIC_DS41: tuple[str, ...] = (
    "mixed_quant_sparse_flash_mla", "quant_lightning_indexer",
    "quant_sparse_lightning_indexer", "indexer_prologue_qw", "indexer_prologue_k",
    "engram_gate", "attn_epilogue", "mega_mla", "mega_full_index", "mega_compressor",
)
KNOWLEDGE_PATHS: tuple[str, ...] = (
    "knowledge/ops/ascendc/apis/appendix/cube_instruction_theoretical_perf_summary.md",
    "knowledge/model/inference/concepts/modules/mhc.md",
    "knowledge/model/inference/concepts/kernels/mhc_hcpre_hcpost.md",
    "knowledge/runtime/concepts/stream_sqcq_async_mechanism.md",
    "knowledge/runtime/concepts/event_synchronization_model.md",
)
HARDWARE_PATHS: tuple[str, ...] = (
    "docs/zh/guide/hardware-model.md", "docs/zh/guide/compute.md",
    "docs/zh/guide/appendix.md",
)
VLLM_PATHS: tuple[str, ...] = (
    "vllm_ascend/compilation/acl_graph.py",
    "vllm_ascend/compilation/breakable_aclgraph.py",
    "vllm_ascend/compilation/updatable_graph.py",
    "vllm_ascend/ops/fused_moe/shared_experts.py",
)
_COMMIT_HEX_RE = re.compile(r"[0-9a-f]{40}\Z")
_SNAPSHOT_PATH = Path(__file__).with_name("catalog_snapshot.json")
_SNAPSHOT: Mapping[str, Any] = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
CORPUS_CONTRACT: Mapping[str, int] = dict(_SNAPSHOT["corpus_contract"])
_HARDWARE_CONSTANTS: Mapping[str, int] = dict(_SNAPSHOT["hardware_constants"])


@dataclass(frozen=True, slots=True)
class TritonRule:
    number: int
    slug: str
    title: str
    category: str
    hazard_tags: tuple[str, ...] = ()


TRITON_RULES: tuple[TritonRule, ...] = tuple(
    TritonRule(
        int(row["number"]),
        str(row["slug"]),
        str(row["title"]),
        str(row["category"]),
        tuple(str(tag) for tag in row.get("hazard_tags", ())),
    )
    for row in _SNAPSHOT["triton_rules"]
)
MFU_GATES: tuple[str, ...] = tuple(str(gate) for gate in _SNAPSHOT["mfu_gates"])
_RULE_NUMBERS: tuple[int, ...] = tuple(rule.number for rule in TRITON_RULES)


def operator_classification(name: str) -> dict[str, str]:
    """Return the packaged catalog coverage metadata for a DSL operator."""
    row = _SNAPSHOT["operators"].get(name)
    if not isinstance(row, Mapping):
        return {
            "catalog_classification": "outside_dsl_catalog_53",
            "derivation_coverage": "not_derived_in_this_pack",
            "runtime_confidence": "analytic_prior_not_source_certification",
        }
    return {
        "catalog_classification": str(row.get("catalog_classification", "outside_dsl_catalog_53")),
        "derivation_coverage": str(row.get("derivation_coverage", "not_derived_in_this_pack")),
        "runtime_confidence": "analytic_prior_not_source_certification",
    }


class DistillError(RuntimeError):
    """The reference corpus is missing or fails a content verification."""


@dataclass(frozen=True, slots=True)
class TritonRuleRow:
    number: int
    title: str
    applicability: str = ""
    trigger: str = ""
    reference: str = ""

    def __post_init__(self) -> None:
        if int(self.number) not in _RULE_NUMBERS:
            raise DistillError(f"invalid Triton rule number: {self.number}")
        for name in ("title", "applicability", "trigger", "reference"):
            object.__setattr__(self, name, str(getattr(self, name)).strip())


@dataclass(frozen=True, slots=True)
class LiveIngestion:
    refs_root: str
    operators: tuple[str, ...]
    operator_sources: Mapping[str, str]
    k3: tuple[str, ...]
    ds41: tuple[str, ...]
    triton_rules: tuple[TritonRuleRow, ...]
    has_fixpipe_doc: bool
    hardware_constants: Mapping[str, int]
    aclgraph_symbols: tuple[str, ...]
    knowledge_files: tuple[str, ...] = ()
    operator_tests: Mapping[str, str] = field(default_factory=dict)
    operator_ci: Mapping[str, str] = field(default_factory=dict)
    source_commits: Mapping[str, str] = field(default_factory=dict)
    content_hashes: Mapping[str, str] = field(default_factory=dict)
    hardware_sources: tuple[str, ...] = ()
    skill_texts: tuple[tuple[str, str], ...] = ()
    source_commit: str = ""
    degraded_offline: bool = False
    live_verified: bool = True
    notes: tuple[str, ...] = ()
    unified_checks: tuple[str, ...] = ("3a", "3b", "3c")

    def __post_init__(self) -> None:
        for name in ("has_fixpipe_doc", "degraded_offline", "live_verified"):
            if not isinstance(getattr(self, name), bool):
                raise DistillError(f"{name} must be bool")

    @property
    def ok(self) -> bool:
        return (
            len(self.operators) == len(STATIC_OPERATORS)
            and len(self.k3) == len(STATIC_K3)
            and len(self.ds41) == len(STATIC_DS41)
            and len(self.triton_rules) == len(TRITON_RULES)
            and self.has_fixpipe_doc
            and len(self.knowledge_files) >= CORPUS_CONTRACT["min_knowledge_files"]
            and len(self.aclgraph_symbols) >= CORPUS_CONTRACT["min_aclgraph_symbols"]
            and bool(self.hardware_constants)
            and bool(self.operator_sources)
            and all(_COMMIT_HEX_RE.fullmatch(self.source_commits.get(repo, "")) is not None
                    for repo in PINNED_COMMITS)
        )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except TimeoutError:
        raise
    except OSError:
        return ""


def _read_json(path: Path) -> Any:
    text = _read_text(path)
    if not text:
        raise DistillError(f"required source missing: {path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DistillError(f"invalid JSON at {path}: {exc}") from exc


def _repo_root(refs: Path, repo: str, vllm: Path) -> Path:
    return vllm if repo == "vllm-ascend" else refs / repo


def _git_dirs(gitdir: Path) -> tuple[Path, Path]:
    commondir = gitdir
    marker = gitdir / "commondir"
    if marker.exists():
        relative = _read_text(marker).strip()
        if relative:
            commondir = (gitdir / relative).resolve()
    return gitdir, commondir


def _git_commit(root: Path) -> str:
    """Resolve a full 40-hex commit from loose, packed, or worktree layouts."""
    dotgit = root / ".git"
    if not dotgit.exists():
        return ""
    if dotgit.is_file():
        text = _read_text(dotgit).strip()
        if not text.startswith("gitdir:"):
            return ""
        gitdir = (root / text.split(":", 1)[1].strip()).resolve()
    else:
        gitdir = dotgit
    gitdir, commondir = _git_dirs(gitdir)
    head = _read_text(gitdir / "HEAD").strip()
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        for base in (gitdir, commondir):
            candidate = base / ref
            if candidate.exists():
                value = _read_text(candidate).strip()
                if _COMMIT_HEX_RE.fullmatch(value):
                    return value
        packed = _read_text(commondir / "packed-refs")
        for line in packed.splitlines():
            if line.startswith("#") or line.startswith("^"):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and _COMMIT_HEX_RE.fullmatch(parts[0]):
                return parts[0]
        return ""
    return head if _COMMIT_HEX_RE.fullmatch(head) else ""


def _sha256(path: Path) -> str:
    try:
        data = path.read_bytes()
    except TimeoutError:
        raise
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def _path_exists(root: Path, relative: str) -> bool:
    candidate = root / relative
    if candidate.exists():
        return True
    if candidate.suffix == ".py":
        return False
    for suffix in (".py", "/README.md", "/__init__.py"):
        if (root / f"{relative}{suffix}").exists():
            return True
    return False


def _parse_rules(text: str) -> tuple[TritonRuleRow, ...]:
    rows: list[TritonRuleRow] = []
    pattern = re.compile(r"^\|\s*(\d{1,2})\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$", re.M)
    for match in pattern.finditer(text):
        num_text, category, feature, priority, reference = match.groups()
        number = int(num_text)
        if number in _RULE_NUMBERS:
            rows.append(TritonRuleRow(number, category, feature, priority, reference))
    dedup = {row.number: row for row in rows}
    missing = set(_RULE_NUMBERS) - set(dedup)
    if missing:
        raise DistillError(f"Triton Index.md is missing rule rows: {sorted(missing)}")
    return tuple(dedup[number] for number in _RULE_NUMBERS)


def _require_marker(text: str, markers: tuple[str, ...], label: str) -> None:
    missing = [marker for marker in markers if marker not in text]
    if missing:
        raise DistillError(f"{label} content missing markers: {missing}")


def _ci_metadata(catalog: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    ci = catalog.get("ci") if isinstance(catalog, Mapping) else None
    rules = ci.get("rules", []) if isinstance(ci, Mapping) else []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name", ""))
        enabled = row.get("enabled", True)
        if not isinstance(enabled, bool):
            raise DistillError("enabled must be bool")
        if name and not enabled:
            result[name] = str(row.get("skip_reason", "") or "temporarily disabled in CI")
    return result


def scan_live(refs_root: str | Path | None = None, vllm_root: str | Path | None = None,
              *, allow_offline: bool = False, source_commit: str | None = None) -> LiveIngestion:
    """Scan and verify the pinned corpus; fail closed unless offline is explicit."""
    if not isinstance(allow_offline, bool):
        raise DistillError("allow_offline must be bool")
    refs = Path(refs_root or os.environ.get("CANNBOT_REFS_ROOT", DEFAULT_REFS_ROOT))
    vllm = Path(vllm_root or os.environ.get("VLLM_ASCEND_ROOT", DEFAULT_VLLM_ROOT))
    required = [refs / "cannbot-skills", refs / "cannbot-dsl", refs / "cannbot-knowledge", vllm]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        if allow_offline:
            return static_ingestion(degraded_offline=True,
                                    notes=(f"offline: missing roots {missing}",))
        raise DistillError(f"required reference repositories missing: {missing}")
    catalog = _read_json(refs / "cannbot-dsl/catalog/operators.json")
    operators_map = catalog.get("operators") if isinstance(catalog, Mapping) else None
    if not isinstance(operators_map, Mapping):
        raise DistillError("cannbot-dsl operators.json has no operator map")
    if len(operators_map) != len(STATIC_OPERATORS) or set(map(str, operators_map)) != set(STATIC_OPERATORS):
        raise DistillError("catalog operator membership does not match the 53-item source list")
    sources, tests, missing_paths = {}, {}, []
    for name, payload in operators_map.items():
        if not isinstance(payload, Mapping):
            raise DistillError(f"operator {name!r} has no metadata mapping")
        source = str(payload.get("source", "")).strip()
        test = str(payload.get("test", "")).strip()
        sources[str(name)] = source
        tests[str(name)] = test
        if not source:
            missing_paths.append(f"{name}: empty source")
        elif not _path_exists(refs / "cannbot-dsl", source):
            missing_paths.append(f"{name}: source {source}")
        if test and not _path_exists(refs / "cannbot-dsl", test):
            missing_paths.append(f"{name}: test {test}")
    if missing_paths:
        preview = missing_paths[:CORPUS_CONTRACT["max_missing_preview"]]
        raise DistillError(f"catalog source/test paths missing: {preview}")
    ci = _ci_metadata(catalog)

    k3 = tuple(str(x) for x in _read_json(refs / "cannbot-dsl/net/k3/network.json")["operators"])
    ds41 = tuple(str(x) for x in _read_json(refs / "cannbot-dsl/net/ds41/network.json")["operators"])
    if k3 != STATIC_K3 or ds41 != STATIC_DS41:
        raise DistillError("K3/DS41 network membership does not match the pinned JSON")

    index_text = _read_text(refs / "cannbot-skills/ops/triton-latency-optimizer/references/Index.md")
    if f"共 **{len(TRITON_RULES)}** 个优化点" not in index_text:
        raise DistillError("Triton Index.md does not declare 33 optimization points")
    rules = _parse_rules(index_text)

    min_hw_chars = CORPUS_CONTRACT["min_hardware_doc_chars"]
    min_src_chars = CORPUS_CONTRACT["min_source_doc_chars"]
    hw_text = {relative: _read_text(refs / "cannbot-dsl" / relative) for relative in HARDWARE_PATHS}
    for relative, text in hw_text.items():
        if len(text) < min_hw_chars:
            raise DistillError(f"hardware source is empty or truncated: {relative}")
    npu_text = _read_text(refs / "cannbot-skills/ops/npu-arch/references/npu-hardware-params.md")
    if len(npu_text) < min_hw_chars:
        raise DistillError("npu-hardware-params.md is empty or truncated")
    _require_marker(hw_text["docs/zh/guide/hardware-model.md"],
                    ("253952", "3 KB", "1 KB", "256 KB", "432", "486"),
                    "hardware-model.md")
    _require_marker(hw_text["docs/zh/guide/compute.md"], ("216 KB", "32 KB"), "compute.md")
    _require_marker(hw_text["docs/zh/guide/appendix.md"], ("128 KB", "L0C"), "appendix.md")
    _require_marker(npu_text, ("Ascend950PR_957b", "Ascend950PR_9589", "131072"),
                    "npu-hardware-params.md")
    hardware_constants = dict(_HARDWARE_CONSTANTS)

    fixpipe = refs / "cannbot-skills/ops/ascendc-api-best-practices/references/api-fixpipe-1c2v.md"
    fixpipe_text = _read_text(fixpipe)
    _require_marker(fixpipe_text, ("L0C", "UB"), "api-fixpipe-1c2v.md")
    if not any(token in fixpipe_text for token in ("subBlockId", "isToUB", "dualDst")):
        raise DistillError("Fixpipe doc lacks lane/dualDst semantics")

    knowledge_files = []
    for relative in KNOWLEDGE_PATHS:
        text = _read_text(refs / "cannbot-knowledge" / relative)
        if len(text) < min_src_chars or "#" not in text:
            raise DistillError(f"knowledge source is empty or headingless: {relative}")
        knowledge_files.append(relative)

    vllm_text = {relative: _read_text(vllm / relative) for relative in VLLM_PATHS}
    for relative, text in vllm_text.items():
        if len(text) < min_src_chars:
            raise DistillError(f"vllm source is empty or truncated: {relative}")
    corpus = "\n".join(vllm_text.values())
    symbols = tuple(symbol for symbol in (
        "ACLGraphWrapper", "BreakableACLGraphWrapper", "UpdatableGraph",
        "super_kernel_optimize", "dcci_after_kernel_end", "207008",
        "join_after_forward", "update_stream",
    ) if symbol in corpus)
    if len(symbols) < CORPUS_CONTRACT["min_aclgraph_symbols"]:
        raise DistillError(f"vllm-ascend ACLGraph symbols missing: {symbols}")

    skill_texts = []
    for relative in ("model/model-infer-superkernel/SKILL.md",
                     "model/model-infer-multi-stream/SKILL.md"):
        text = _read_text(refs / "cannbot-skills" / relative)
        if len(text) < min_src_chars:
            raise DistillError(f"skill text missing or truncated: {relative}")
        skill_texts.append((relative, text))
    if "仅支持 ge_graph" not in skill_texts[0][1] and "ge_graph" not in skill_texts[0][1]:
        raise DistillError("superkernel skill no longer states its ge_graph constraint")

    commits = {repo: _git_commit(_repo_root(refs, repo, vllm)) for repo in PINNED_COMMITS}
    bad = [repo for repo, value in commits.items() if not _COMMIT_HEX_RE.fullmatch(value or "")]
    if bad:
        raise DistillError(f"cannot resolve full git commits for: {bad}")
    if source_commit is not None and source_commit != commits["cannbot-dsl"]:
        raise DistillError("source_commit override does not match the live DSL repository")
    files = (
        [refs / "cannbot-dsl/catalog/operators.json",
         refs / "cannbot-dsl/net/k3/network.json", refs / "cannbot-dsl/net/ds41/network.json"]
        + [refs / "cannbot-dsl" / relative for relative in HARDWARE_PATHS]
        + [refs / "cannbot-dsl/docs/zh/guide/compiler-options.md",
           refs / "cannbot-skills/ops/triton-latency-optimizer/references/Index.md",
           fixpipe, refs / "cannbot-skills/ops/triton-mfu-analyzer/SKILL.md",
           refs / "cannbot-skills/ops/cannbot-dsl-op-design/references/stage-cost-model.md"]
        + [refs / "cannbot-skills" / relative for relative, _ in skill_texts]
        + [refs / "cannbot-knowledge" / relative for relative in KNOWLEDGE_PATHS]
        + [vllm / relative for relative in VLLM_PATHS]
    )
    hashes = {}
    for path in files:
        if not path.exists():
            raise DistillError(f"required provenance file missing: {path}")
        for repo, root in (("cannbot-skills", refs / "cannbot-skills"),
                           ("cannbot-dsl", refs / "cannbot-dsl"),
                           ("cannbot-knowledge", refs / "cannbot-knowledge"),
                           ("vllm-ascend", vllm)):
            if str(path).startswith(str(root) + os.sep):
                hashes[f"{repo}/{path.relative_to(root)}"] = _sha256(path)
                break
    return LiveIngestion(
        refs_root=str(refs), operators=tuple(map(str, operators_map)),
        operator_sources=sources, operator_tests=tests, operator_ci=ci,
        k3=k3, ds41=ds41, triton_rules=rules, has_fixpipe_doc=True,
        hardware_constants=hardware_constants, aclgraph_symbols=symbols,
        knowledge_files=tuple(knowledge_files), source_commits=commits,
        content_hashes=hashes, hardware_sources=HARDWARE_PATHS,
        skill_texts=tuple(skill_texts),
        source_commit=source_commit or commits["cannbot-dsl"],
        live_verified=True,
    )


def static_ingestion(degraded_offline: bool = True,
                     notes: tuple[str, ...] = ()) -> LiveIngestion:
    """Offline snapshot; never claims live verification."""
    if not isinstance(degraded_offline, bool):
        raise DistillError("degraded_offline must be bool")
    snapshot = _SNAPSHOT["operators"]
    sources = {name: str(row["source"]) for name, row in snapshot.items()}
    return LiveIngestion(
        refs_root="offline-snapshot", operators=STATIC_OPERATORS,
        operator_sources=sources,
        operator_tests={name: str(row.get("test", "")) for name, row in snapshot.items()},
        operator_ci={}, k3=STATIC_K3, ds41=STATIC_DS41,
        triton_rules=tuple(TritonRuleRow(number, f"rule-{number}")
                           for number in _RULE_NUMBERS),
        has_fixpipe_doc=True,
        hardware_constants=dict(_HARDWARE_CONSTANTS),
        aclgraph_symbols=("ACLGraphWrapper", "BreakableACLGraphWrapper",
                          "UpdatableGraph", "super_kernel_optimize",
                          "dcci_after_kernel_end", "207008", "join_after_forward",
                          "update_stream"),
        knowledge_files=KNOWLEDGE_PATHS,
        source_commits=dict(PINNED_COMMITS), content_hashes={},
        hardware_sources=HARDWARE_PATHS,
        skill_texts=(("model/model-infer-superkernel/SKILL.md",
                      "SuperKernel 仅支持 ge_graph 模式，不支持 aclgraph。"),),
        source_commit=PINNED_COMMITS["cannbot-dsl"],
        degraded_offline=degraded_offline, live_verified=False,
        notes=notes or ("offline snapshot; source content hashes unavailable",),
    )


__all__ = [
    "CORPUS_CONTRACT", "DEFAULT_REFS_ROOT", "DEFAULT_VLLM_ROOT", "DistillError",
    "HARDWARE_PATHS", "KNOWLEDGE_PATHS", "LiveIngestion", "MFU_GATES",
    "PINNED_COMMITS", "STATIC_DS41", "STATIC_K3", "STATIC_OPERATORS",
    "TRITON_RULES", "TritonRule", "TritonRuleRow", "VLLM_PATHS",
    "operator_classification", "scan_live", "static_ingestion",
]
