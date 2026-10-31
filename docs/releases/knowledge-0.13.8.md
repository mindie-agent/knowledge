# mindie-knowledge 0.13.8：知识单元语义修订自动演进与多宿主发行版选择支持

**中文** · [English](https://github.com/mindie-agent/knowledge/blob/v0.13.8/docs/releases/knowledge-0.13.8.en.md)

- **索引构建期自动递增语义版本（`build_routing_index`）**：在生成或更新 `index/routing-manifest.json` 与 `index/shard-topology.json` 时，自动对比上一轮索引中的单元内容摘要（`content_sha256`）与 `revision`；若单元内容发生变更但 `revision` 未递增，`build_routing_index`（`check=False`）自动将对应 `.md` 文件的 `revision` 递增为 `old_revision + 1`，而只读校验模式（`check=True`）显式报告 `revision_not_advanced` 错误，从源头杜绝人工或 Agent 修改语料时漏升版本号。
- **发布校验器基线单调性门禁（`publication_check.py`）**：`validate(..., base_revision=...)` 对 `mindie-publication-contract/2` 知识库强制校验候选提交相对 `base_revision` 的所有同名知识单元满足 `revision` 单调不减、内容变更时 `revision` 严格递增，在语料仓 PR 与推送阶段直接拦截漏升修订的提交。
- **客户端增量同步单调修复兼容（`GradedRepository.install_feed`）**：允许相同 `content_sha256` 下的 `revision` 单调前移（`unit.revision >= old.revision`），同时继续拒绝任何版本回退（`unit.revision < old.revision`）或内容变更未升版（`old.content_sha256 != unit.content_sha256 and unit.revision <= old.revision`）。
- **新增 Antigravity 与 DSH 官方发行版仓库映射（`releases.py` / `releases_io.py`）**：在 `REPOSITORIES` 与 `ALLOWED_HOSTS` 中登记 `antigravity`（`mindie-agent/mindie-agent-agy`）与 `dsh`（`mindie-agent/mindie-agent-dsh`），支持五端宿主统一通过 `validate_compatibility` 与 `select_release` 完成兼容性校验与正式版筛选。
