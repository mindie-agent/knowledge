"""MindIE community sharing: deterministic contributor-side publication.

Public API for the knowledge core:

- :func:`submit_batch` — publish one validated contribution batch.
- :func:`reconcile_batch` — bounded read-only reconciliation of one batch.

Repository-side review and optional Skill proposals are the EXTERNAL Grok
Bot software's operations under maintainer deployment (see
``docs/repository-bot-contract.md``); this package ships no bot runtime, no
model bridge and no scheduler. ``skill_validation`` offers deterministic
data-only checks for Skill package bytes.
"""

from .publish import inspect_batch, reconcile_batch, submit_batch

__all__ = ["submit_batch", "reconcile_batch", "inspect_batch"]
