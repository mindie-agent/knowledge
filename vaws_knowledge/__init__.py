"""Local Markdown references, optional retrieval and public contribution."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from importlib import import_module

from vaws_knowledge import corpus

__all__ = ["corpus", "package_version", "redact"]


def __getattr__(name: str):
    # Reading bundled Markdown remains a stdlib-only operation, even when a
    # distribution is installed with --no-deps for offline corpus access.
    if name == "redact":
        module = import_module("vaws_knowledge.redact")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def package_version() -> str:
    """Installed distribution version. The package version is the contract."""

    try:
        return version("vaws-knowledge")
    except PackageNotFoundError:
        return "0.7.5"
