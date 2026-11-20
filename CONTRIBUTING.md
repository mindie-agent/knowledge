# 参与贡献（Contributing to `mindie-knowledge`）

**中文** | [English](CONTRIBUTING.en.md) · [返回仓库首页](README.md)

欢迎参与 **MindIE Knowledge**（`mindie-agent/knowledge`，仓库分类：`state-service`）的开发与改进！本仓库负责 MindIE Agent 的分级领域知识检索（`L1–L4`）、分层分片路由、无损任务材料库（`MaterialStore`）、双层闭锁式隐私脱敏、统一同意书状态库（`mindie-consent/1`）及社区 PR 外发对账。

---

## 1. 核心工程准则（`state-service` 四大铁律）

1. **`SVC-CONSENT` — 公开检索零预绑定与严格采集/外发授权边界（`P2.7` / `P2.8` & `P2.1`）**：
   公开知识读取与同步（`knowledge_query`、`knowledge_explain`、本地 `knowledge_feedback` 与 `knowledge_sync`）基于标准 MCP `tools/call` 开箱即用，零前置会话绑定门禁（`P2.7` / `P2.8`）；社区共享默认关闭（Opt-in），`Stop` 会话采集、`knowledge_distill` 经验提炼与 GitHub PR 外发严格受 `mindie-consent/1`（单调 `choice_revision`，支持 `contribute` / `local-only` / `read-only`）、授权 `project_roots` 与活跃采集租约约束（`origin_tier="local-only"` 永不外发）。撤回授权时立即停止未发送外发项，重新开启时绝不回溯补发未授权期间的历史数据。
2. **`SVC-STATE-COMPAT` — 状态 Schema 前后向兼容与纯知识边界**：
   保持 `state_layout.py`（`FORMAT = 1`、`state-v4.sqlite3`）、`MaterialStore` 双阶段原子指针（`current.json`）与 `mindie-graded-unit/1` 回滚安全。RSI 演化循环（`rsi_loop`、`experiment_ledger`、`rsi_crossover`）以 `mindie_harness.rsi` 为唯一演进源，本仓对应模块仅作为平滑升级桥接层。严禁向任意 `Path.cwd()` 写出查询审计日志。
3. **`SVC-LIFETIME` — 单调预算、软链/Worktree 友好与模块内聚（`< 800` 行）**：
   `Stop` 准入全流程服从单一单调外层截止时间（不做同步大模型调用或重型全文解析，预算不足时诚实返回降级原因）。文件读取校验 `realpath` 边界（兼容 dotfiles 软链与 Git 关联 Worktree `.git` 文件）。核心 Python 模块保持 `< 800` 行并具备 `[Role]` / `[Invariants]` / `[Timeouts & Locks]` / `[Side-Effect Policy]` 头部说明；顶层 `import mindie_knowledge` 保持懒加载（`< 20ms`）。
4. **`SVC-EXPORT` — 双层闭锁脱敏与外部副作用诚实回执**：
   外发前强制通过 Gitleaks + `mindie_knowledge.redact`（`r4`）双重扫描。严格区分 `completed`、`failed` 与外部写入结果未知（`unknown` / `outcome_unknown`）：遇到网络超时仅做只读对账，绝不盲目重放外部写或付费模型调用；`except OSError:` 前必须显式保留 `except TimeoutError: raise`；远端主操作成功后若本地回执写入失败，必须保留远端成功回执并附带告警。

---

## 2. 本地隔离测试与必跑门禁

运行测试时请确保隔离宿主环境变量（`tests/conftest.py` 已内置沙箱 fixture，切勿污染宿主机 `~/.config` 或 `~/.local`）：

```bash
# 1. 安装可编辑运行时与测试依赖
python3 -m pip install -e '.[test]'

# 2. 运行隔离单元与契约测试套件（排除常驻实机 NPU 用例）
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python3 -B -m pytest -q -n auto --dist worksteal -m "not live_npu"

# 3. 扫描公开文档与技能文件，确保零敏感数据命中（文档示例请使用 <user>@<host> 或 192.0.2.x）
python3 -B -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills

# 4. 校验 Unix LF 换行符与 state-service PR 自查清单
python3 .github/scripts/check_lf_eol.py
python3 .github/scripts/check_pr_checklist.py --profile state-service --body-file <pr_body.md>
```

---

## 3. 提交 PR 说明

提交或更新 PR 时，请按 [`.github/pull_request_template.md`](.github/pull_request_template.md) 勾选 `SVC-CONSENT`、`SVC-STATE-COMPAT`、`SVC-LIFETIME`、`SVC-EXPORT` 四项检查并填写具体核验依据（纯文档等不适用项请注明 `not-applicable: <具体原因>`，切勿填写空白或笼统的 `N/A`）。
