# mindie-knowledge 0.13.8: Automatic Semantic Revision Advancement & Multi-Harness Release Selection

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.8/docs/releases/knowledge-0.13.8.md) · **English**

- **Automatic Semantic Revision Advancement During Index Rebuild (`build_routing_index`)**: Compares unit `content_sha256` digests and `revision` integers against the previous `index/routing-manifest.json` and `index/shard-topology.json`. When a unit's content changes without advancing `revision`, `build_routing_index` (`check=False`) automatically increments `revision` to `old_revision + 1` in the unit file before writing derived routing artifacts, while `check=True` reports an explicit `revision_not_advanced` error.
- **Publication Validator Base-Revision Monotonicity Gate (`publication_check.py`)**: `validate(..., base_revision=...)` enforces that every `mindie-publication-contract/2` unit present in `base_revision` preserves or advances its `revision` and strictly advances `revision` whenever `content_sha256` changes.
- **Monotonic Revision Repair Compatibility (`GradedRepository.install_feed`)**: Permits monotonic revision advances under identical `content_sha256` (`unit.revision >= old.revision`) while continuing to reject revision regressions (`unit.revision < old.revision`) or unbumped content modifications.
- **Antigravity & DSH Official Release Repository Registration (`releases.py` / `releases_io.py`)**: Registers `antigravity` (`mindie-agent/mindie-agent-agy`) and `dsh` (`mindie-agent/mindie-agent-dsh`) in `REPOSITORIES` and `ALLOWED_HOSTS`.
