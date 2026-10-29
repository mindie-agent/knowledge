# mindie-knowledge 0.13.6: Shared Explain Budgets and Managed Component Version Floors

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.6/docs/releases/knowledge-0.13.6.md) · **English**

A single graded `ref` no longer bypasses the response budget. Single and batch explanations use the complete prerequisite closure and count compatibility fields such as `content` and `frontmatter` before final selection. Units that do not fit are explicitly unloaded; executable rules are never truncated. A target already visible at its current revision still retains missing prerequisites, or reports them as `budget_excluded`. Dependencies use actual revision references, and `known_refs` only omits complete content still visible at that same revision.

The shared update API accepts native owners' required components and version floors from external Python environments. Shared helpers recognize standard `python -m` component commands and read their metadata within one 15-second budget across interpreters. Metadata prevents downgrades without inventing source SHAs. Newly managed components still require formal Releases validated against existing identity, asset, and benchmark contracts. Native owners retain candidate construction, idle activation, and configuration comparison; custom wrappers and console scripts without proven ownership remain unchanged.

Kimi source baselines apply `runtime-active-overrides.txt` over immutable bootstrap requirements while retaining official repository and full SHA checks. Version 0.13.5 was used only for development integration and was not released. Version 0.13.6 assigns a new release identity to the source and its matching release notes.

The merged functional commit passed 120 focused local tests with one skip for an external knowledge checkout outside the test workspace. Linux, macOS, Windows contract checks and package checks also passed for that commit. The formal release workflow still validates this exact tag's dependencies, full local contracts, graded Feed functional workload, and tokenization microbenchmark, publishing the resulting evidence with the release.

This version does not claim lower total cost for complex tasks or completed upgrades of all four installed native hosts. The complex-task non-regression threshold was not met in the existing small CPU pilot; failures, exhausted budgets, and missing measurements remain preserved. Automatic upgrade and installed-version readback require separate acceptance with the matching formal plugin releases.
