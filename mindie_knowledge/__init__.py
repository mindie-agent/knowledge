"""Local domain knowledge, CANNBot/ACLGraph distiller, and experience loop.

[Role]
Expose package version resolution and lazy access to privacy redaction, the
CANNBot/ACLGraph KnowledgeUnit schema, TorchAir->ACLGraph normalization engine,
and 3-stack corpus distiller without eager submodule loading.
[Invariants]
`package_version` resolves distribution metadata or the source baseline cleanly;
all exported submodule and symbol bindings in `__all__` resolve lazily via
`__getattr__` so `import mindie_knowledge` stays `< 20ms` with zero heavy or
cross-package imports.
[Timeouts & Locks]
Pure metadata and lazy stdlib module imports; no blocking I/O, locks, or subprocesses.
[Side-Effect Policy]
Import-time read-only package metadata lookup only; zero file writes or engine warmup.
"""

from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS: dict[str, tuple[str, str | None]] = {
    "DistillError": ("mindie_knowledge.cannbot_aclgraph_distiller", "DistillError"),
    "KnowledgeUnit": ("mindie_knowledge.knowledge_unit", "KnowledgeUnit"),
    "KnowledgeUnitError": ("mindie_knowledge.knowledge_unit", "KnowledgeUnitError"),
    "LiveIngestion": ("mindie_knowledge.cannbot_aclgraph_distiller", "LiveIngestion"),
    "NORMALIZATION_RULES": ("mindie_knowledge.cannbot_aclgraph_distiller", "NORMALIZATION_RULES"),
    "NormalizationResult": ("mindie_knowledge.cannbot_aclgraph_distiller", "NormalizationResult"),
    "NormalizationRule": ("mindie_knowledge.cannbot_aclgraph_distiller", "NormalizationRule"),
    "distill_live": ("mindie_knowledge.cannbot_aclgraph_distiller", "distill_live"),
    "distill_static": ("mindie_knowledge.cannbot_aclgraph_distiller", "distill_static"),
    "distill_units": ("mindie_knowledge.cannbot_aclgraph_distiller", "distill_units"),
    "normalize_text": ("mindie_knowledge.cannbot_aclgraph_distiller", "normalize_text"),
    "normalize_texts": ("mindie_knowledge.cannbot_aclgraph_distiller", "normalize_texts"),
    "redact": ("mindie_knowledge.redact", None),
    "scan_live": ("mindie_knowledge.cannbot_aclgraph_distiller", "scan_live"),
    "stack_counts": ("mindie_knowledge.cannbot_aclgraph_distiller", "stack_counts"),
    "static_ingestion": ("mindie_knowledge.cannbot_aclgraph_distiller", "static_ingestion"),
    "validate_unit": ("mindie_knowledge.knowledge_unit", "validate_unit"),
    "validate_units": ("mindie_knowledge.knowledge_unit", "validate_units"),
    "write_units": ("mindie_knowledge.cannbot_aclgraph_distiller", "write_units"),
}

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
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module = import_module(module_name)
    value = module if attr_name is None else getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


def package_version() -> str:
    """Installed distribution version. The package version is the contract."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("mindie-knowledge")
    except TimeoutError:
        raise
    except PackageNotFoundError:
        return "0.13.24"
