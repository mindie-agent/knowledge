# mindie-knowledge 0.13.9：新增腾讯 WorkBuddy 官方发行版仓库与兼容性映射

**中文** · [English](https://github.com/mindie-agent/knowledge/blob/v0.13.9/docs/releases/knowledge-0.13.9.en.md)

- **新增 WorkBuddy 官方插件仓库映射（`releases.py` / `releases_io.py`）**：在 `REPOSITORIES` 与 `_COMPATIBILITY_PATHS` 中正式登记 `"workbuddy": "mindie-agent/mindie-agent-wb"`（兼容性清单路径 `harness-compatibility.json`），支持 WorkBuddy 宿主统一通过 `validate_compatibility`、`validate_candidate_source` 与 `select_release` 完成兼容性校验与正式版筛选。
