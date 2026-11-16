"""[Role] Shared storage lifecycle: leases, two-phase retirement, legacy sweep.

[Invariants]
- ``leases`` is the only liveness authority; ``lifecycle`` is the only retirer;
  ``legacy`` unions every host reference before asking ``lifecycle`` to reclaim.
- Public helpers preserve the caller's environment and never invent a fallback
  state machine. A failed atomic rename leaves the source directory intact.

[Timeouts & Locks]
- Every entry point is bounded by a caller budget; leases are non-blocking OS
  locks released by the kernel on process exit.

[Side-Effect Policy]
- Re-exports only; importing this package performs no I/O, lock or deletion.
"""
from __future__ import annotations

from .leases import (
    RuntimeLease,
    acquire_runtime_lease,
    is_lease_held,
    is_path_used_by_live_process,
    live_process_prefixes,
)
from .lifecycle import (
    configure_same_volume_uv_cache,
    fs_path,
    is_protected_path,
    prune_component_runtimes,
    retire_directory,
    same_volume_cache_dir,
    sweep_trash,
)
from .legacy import (
    collect_global_protected_roots,
    owning_dir,
    sweep_legacy_installations,
)

__all__ = [
    "RuntimeLease",
    "acquire_runtime_lease",
    "collect_global_protected_roots",
    "configure_same_volume_uv_cache",
    "fs_path",
    "is_lease_held",
    "is_path_used_by_live_process",
    "is_protected_path",
    "live_process_prefixes",
    "owning_dir",
    "prune_component_runtimes",
    "retire_directory",
    "same_volume_cache_dir",
    "sweep_legacy_installations",
    "sweep_trash",
]
