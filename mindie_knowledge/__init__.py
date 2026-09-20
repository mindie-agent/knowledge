"""Local domain knowledge and experience loop with optional community sharing."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from importlib import import_module

__all__ = ["package_version", "redact"]


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
    except PackageNotFoundError:
        return "0.8.0"
