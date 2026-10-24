# mindie-knowledge 0.13.3：独立组件正式版发现、旁路隔离环境构建与整包 Token 预算估算

**中文** | [English](https://github.com/mindie-agent/knowledge/blob/v0.13.3/docs/releases/knowledge-0.13.3.en.md)

`mindie-knowledge 0.13.3` 新增了共享运行时组件的独立 GitHub Release 发现、基准门禁校验与旁路（Side-by-Side）隔离 `uv` 环境构建能力（`#101`），同时完成了 `query`/`explain`/`explore` 整包 Token 预算估算、完整探索前提挂载、单事务去重投递记账与混合检索源索引绑定（`#97`、`#99`、`#100`），并锁定正式发布的 `mindie-diagnostics 0.5.2`（`801a01b2789aed7f58e971a909205a7d40b61077`）。

---

## 1. 核心变更

- **独立组件正式 Release 发现与多重门禁校验（`component_releases.py` & `runtime_inventory.py`）**：
  - 支持直接从 8 个官方组件仓库（`mindie-knowledge`、`remote-dev`、`mindie-diagnostics`、`mindie-profiler`、`mindie-sim`、`npu-top`、`mindie-coordinator`、`mindie-bench`）读取最新正式版 Release（`/releases/latest`），对当前已安装组件与插件声明基线（`runtime-requirements.txt` 或 `pyproject.toml`）取最高稳定版本，绝不降级；同版本出现不同源码提交哈希时立即报错拒绝。
  - 逐项核验正式 Release 必需资产（对应版本的 `.whl`、`.tar.gz`、`SHA256SUMS`、`benchmark-results.json`、`benchmark-results.md` 与 `benchmark-SHA256SUMS`，且文件大小均 `> 0`），将 Tag 解引用至不可变 40 位 Commit SHA 并核对源码 `pyproject.toml` 包名与版本；实际下载 `SHA256SUMS` 与 `benchmark-results.json` 校验 SHA256 摘要，要求 `release_gate is True`、版本一致且测量前后工作区干净（`source` 与 `source_after` 的 `revision` 一致且 `dirty is False`）。
- **旁路隔离 `uv` 运行时构建与有界 `ETag` 缓存（`component_runtime.py` & `release_http.py`）**：
  - 在活动环境之外按组件修订与平台 ABI 生成独立目录（`generation/source` 与 `generation/runtime/.venv` 并排），以验证通过的精确 Git Commit SHA 执行 `uv sync --project <runtime> --no-editable --no-dev`，并通过隔离进程 `python -I runtime_inventory.py <components.json>` 校验 `uv.lock` 完整 SHA256 与各组件 `direct_url.json` 提交哈希后原子写出 `components.json`（`mindie-component-runtime/1`）。已完成校验的同代候选环境可直接复用，原生 API 探针与空闲切换仍由各宿主插件适配器各自持有。
  - 插件与组件更新共享有界公开 HTTP 缓存（上限 `128` 条目 / `64 MiB`，不可变对象 TTL `86400s`，可变 Release 接口有 Token `300s` / 无 Token `900s`，支持 `ETag` 条件重验），离线或请求失败显式报错，绝不用过期缓存伪装成功；更新检查与环境构建全程不主动启动知识后台服务，也不改变会话级手动 `/mindie-agent` 激活门禁。
- **整包 Token 预算估算、完整探索前提与单事务去重投递记账（`#97`、`#99`、`#100`）**：
  - `graded/budget.py` 对 `query`、`explain` 与 `explore` 的完整返回载荷执行统一整包 Token 预算估算与裁剪；当预算低于协议最小信封估算开销时如实标记 `budget_met = False` 并返回收敛后的 `minimum_tokens`。
  - `graded/explore.py` 移除探索前提正文的固定字符截断，为每个挂载单元绑定标准 `mindie://<domain>/units/<id>@<revision>` 引用；超预算时按 `L4 -> L2 -> L1` 顺序整单元卸载并记入 `excluded_units`、同步刷新子 Agent 事实包。
  - 单次响应内的知识单元投递计数在单个 SQLite 写事务内去重落盘（继承规则仅注册可读权限、不重复计次），混合检索直接绑定当次查询的内存路由索引快照，消除并发更新知识源时的索引漂移。

---

## 2. 验证与验收边界

- **已核验测试结果**：
  - 本地全量测试套件：`1555 passed, 22 skipped, 3 deselected, 3 subtests`；
  - 组件更新与运行时核验专项测试：`58 passed`；
  - 预算估算、探索前提、去重记账与手动入口回归测试（8 个模块）：`105 passed, 1 skipped`（`13.02s`）；
  - GitHub Actions 3 个 CI 工作流（含 `mindie_knowledge.redact --check`）全部通过。
- **宿主验收边界说明**：
  - 本版本已锁定正式版 `mindie-diagnostics 0.5.2`（`801a01b2789aed7f58e971a909205a7d40b61077`），并提供共享组件独立 Release 发现与旁路运行时构建能力；四个宿主客户端（Codex、Claude Code、Kimi Code、DSH）的原生新会话自动更新端到端验收在宿主侧后续版本中独立验证，不在本组件发布说明中宣称已完成四端新会话实机验收。
