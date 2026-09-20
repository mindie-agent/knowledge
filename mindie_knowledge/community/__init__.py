"""MindIE community sharing: GitHub contribution and repository review.

Public API for the knowledge core:

- :func:`submit_batch` — publish one validated contribution batch.
- :func:`reconcile_batch` — bounded read-only reconciliation of one batch.

The review runner and Skill consolidation are maintainer-side automation,
reachable through ``python -m mindie_knowledge.community``.
"""

from .publish import reconcile_batch, submit_batch

__all__ = ["submit_batch", "reconcile_batch"]
