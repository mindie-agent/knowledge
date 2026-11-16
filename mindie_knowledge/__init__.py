"""Local domain knowledge, CANNBot/ACLGraph distiller, and experience loop.

[Role]
Expose package version resolution, lazy privacy redaction, and the dependency-free
CANNBot/ACLGraph KnowledgeUnit schema, TorchAir->ACLGraph normalization engine,
and 3-stack corpus distiller.
[Invariants]
`package_version` resolves distribution metadata or the source baseline cleanly;
`redact` loads on demand via `__getattr__`; distiller and KnowledgeUnit APIs
remain 100% stdlib-only with zero cross-package simulator dependencies.
[Timeouts & Locks]
Pure metadata and stdlib module imports; no blocking I/O, locks, or subprocesses.
[Side-Effect Policy]
Import-time read-only access to packaged snapshot metadata only; no file writes.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

from .cannbot_aclgraph_distiller import (
    DistillError,
    LiveIngestion,
    NORMALIZATION_RULES,
    NormalizationResult,
    NormalizationRule,
    distill_live,
    distill_static,
    distill_units,
    normalize_text,
    normalize_texts,
    scan_live,
    stack_counts,
    static_ingestion,
    write_units,
)
from .knowledge_unit import (
    KnowledgeUnit,
    KnowledgeUnitError,
    validate_unit,
    validate_units,
)

__all__ = [
    "DistillError",
    "KnowledgeUnit",
    "KnowledgeUnitError",
    "LiveIngestion",
    "NORMALIZATION_RULES",
    "NormalizationResult",
    "NormalizationRule",
    "distill_live",
    "distill_static",
    "distill_units",
    "normalize_text",
    "normalize_texts",
    "package_version",
    "redact",
    "scan_live",
    "stack_counts",
    "static_ingestion",
    "validate_unit",
    "validate_units",
    "write_units",
]


def __getattr__(name: str):
    if name == "redact":
        module = import_module("mindie_knowledge.redact")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def package_version() -> str:
    """Installed distribution version. The package version is the contract."""
    try:
        return version("mindie-knowledge")
    except TimeoutError:
        raise
    except PackageNotFoundError:
        return "0.13.22"
