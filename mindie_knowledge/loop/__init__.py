"""MindIE domain-scoped knowledge, experience, and use-effect loop package.

[Role]
Provide the local capture, retrieval, activation, and background maintenance
runtime for domain-scoped knowledge loops.
[Invariants]
All runtime state stays isolated under configured data/config roots with SQLite
WAL and bounded working sets.
[Timeouts & Locks]
Import-time package namespace only; operational budgets live in submodules.
[Side-Effect Policy]
Side-effect-free package initialization.
"""
