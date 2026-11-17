## 变更摘要 (Summary)

- 

## 设计原则 Review (Pre-PR Checklist — `state-service`)

<!-- 请核对以下四项 state-service 契约，并在下方提供逐项依据（不适用项请写 `not-applicable: 具体原因`，勿填裸 `N/A` 或 `TODO`）。全仓文件须保持 Unix LF 换行。 -->

- [ ] **SVC-CONSENT** — `P2.1` 显式会话租约（`entry_enabled = 1`）与 `mindie-consent/1`（`choice_revision`）门禁完备；撤回同意书立即停止待发项且重新开启零历史补发。
- [ ] **SVC-STATE-COMPAT** — `state-v4.sqlite3`、`MaterialStore`（`current.json` + `DATA_FORMAT_V1`）、`mindie-graded-unit/1` 与 v1/v2 `runtime_inventory` 保持回滚兼容；SQLite WAL + `busy_timeout=5000` 仅在 `open_database` 配置一次；零非受管 `Path.cwd()` 写入。
- [ ] **SVC-LIFETIME** — `Stop` 准入服从单一单调外层 Deadline（零同步模型/重型解析阻塞）；支持合法软链边界校验与 Git Worktree `.git` 文件；顶层懒加载 `< 20ms`，核心模块 `< 800` 行且含标准四段契约头，Windows 子进程显式携带 `CREATE_NO_WINDOW`。
- [ ] **SVC-EXPORT** — 双层闭锁脱敏（Gitleaks + `r3`）完备；`except OSError:` 前置 `except TimeoutError: raise`；外部写/付费调用区分 `completed` / `failed` / `unknown`（只读对账不盲目重放，远端成功后本地记账异常保留成功回执）。

## 自查证据 (Evidence)

- **SVC-CONSENT**: 
- **SVC-STATE-COMPAT**: 
- **SVC-LIFETIME**: 
- **SVC-EXPORT**: 

## 验证结果 (Verification)

- 
