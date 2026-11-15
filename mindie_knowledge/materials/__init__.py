"""Current Markdown task packages and their replaceable ReMe search index.

[Role]
Export `MaterialStore`, `package_from_files`, and `validate_package_files` for
validated Markdown task-package storage and retrieval.
[Invariants]
Markdown package files on disk remain the source of truth; search indexes are
rebuildable derived caches.
[Timeouts & Locks]
Import-time re-exports only; SQLite/filesystem budgets live in `.store`.
[Side-Effect Policy]
Side-effect-free package initialization.
"""

from .store import MaterialStore, package_from_files, validate_package_files

__all__ = ["MaterialStore", "package_from_files", "validate_package_files"]
