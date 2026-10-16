# MindIE Knowledge

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-0284C7.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-0F766E.svg)](LICENSE)
[![Corpus License: CC-BY-4.0](https://img.shields.io/badge/corpus-CC--BY--4.0-0D9488.svg)](LICENSE-CORPUS)

中文 | [English](README.en.md)

**MindIE Knowledge**（Python 包名 `mindie-knowledge`，模块命名空间 `mindie_knowledge`）是 [MindIE Agent](https://github.com/mindie-agent/mindie-agent) 的共享领域知识与任务经验闭环运行时组件。它负责已准入任务的有界公开对话采集、增量分块摘要与本地 BM25 检索、经严格隐私脱敏的可选 Git 社区共享，以及来自权威公共仓库的只读知识包同步与独立有用性反馈。

本项目完整继承并遵循 [MindIE Agent / VAWS 九条设计原则](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md)：以完成用户实际目标的总成本衡量效率，保持知识仅供参考、工具按需介入、边界单一明确，并坚持错误可见与禁止静默降级。

---

## 核心特性

- **权威公开知识包同步（Public Feed Sync）**：通过确定性、零模型调用的 Git 管道拉取上游知识库（遵循 `publication-contract.json` 契约与 SHA-256 固定校验），以单文件流式校验与原子切换更新本地只读视图。
- **有界增量采集与本地检索（Local Material & BM25 Retrieval）**：正文以普通 Markdown 文件存储在本地 `materials/` 目录，`state-v4.sqlite3` 仅保存头部、指针、游标与回执。固定版本的 ReMe 组件提供本地 Markdown 分块、文件引用图与 BM25 检索，消费端检索不启动任何常驻守护进程、不调用模型、无需外部账号配置。
- **双层闭锁隐私脱敏（Fail-Closed Privacy Redaction）**：仅在用户显式开启社区贡献时执行采集。入库前与外发前均强制经过固定版本 Gitleaks 与 `mindie_knowledge.redact`（规则集 `r3`）确定性脱敏扫描；任何凭据、私钥片段、内网地址、主机名或个人路径命中均闭锁拦截。
- **确定性发件箱与只读对账（Deterministic Outbox & Reconciliation）**：自动将本地草稿修订与可发布反馈合并为 `mindie-contribution/1` 批次并提交 PR。对网络中断或结果未知的外部写入仅执行带持久化退避的只读远程对账，绝不盲目重放写操作或重复计费模型调用。
- **独立有用性反馈（Independent Usefulness Feedback）**：支持针对具体观测修订（`mindie://DOMAIN/<task-id>@<observed-revision>`）记录可选的 `up` / `down` 评价与单行原因，不设裁判模型或投票权重，也不把投票数当作事实认证。

---

## 架构与数据分层

`mindie-knowledge` 将运行时数据严格划分为三个职责正交的层次，位于 `<root>/<domain>/state-v4/` 下：

| 层次 | 权威存储位置 | 核心职责与边界 | 模型与网络约束 |
|---|---|---|---|
| **公开订阅层（Public Feed Layer）** | 浅层 Git 缓存 + 已验证的 `tasks/<task-id>/` 包视图 | 追踪上游内容仓库分支的不可变提交，验证 `publication-contract.json`、`mindie-material-task/1` 清单（内嵌 `mindie-entry/3` 头部）及 `mindie-material-block/1` 分块文件，完成后原子切换当前可见版本；上游删除自动转化为本地撤回标记。 | 零模型调用；仅通过 `git` 只读拉取并通过单个持久 `git cat-file --batch` 流式校验。 |
| **私有本地层（Local Material & Index Layer）** | `materials/`（Markdown 正文）+ `state-v4.sqlite3`（元数据/回执） | 承载 `public-transcript` 增量采集或显式历史导入生成的无损 UTF-8 分块（每块上限 16 KiB）。由 Harness 提供的 `summary_command`（LangMem 0.0.30）按有界批次（最多 8 块 / 64 KiB）生成可证伪的块标题、摘要与导航；ReMe 检查点按压缩行缓存分块词频。 | 消费端检索（`knowledge_query` / `knowledge_explain`）零模型调用、零网络请求；摘要仅对新增分块调用一次有界原生 worker。 |
| **发件箱与共享层（Outbox & Sharing Layer）** | `outbox/`（私有冻结暂存包 + 发布账本） | 仅在 `mindie-consent/1` 与 `mindie-community-config/1` 双重门禁同时满足时激活。将已完成全块索引的任务包与 `feedback/*.json`（`mindie-feedback/1`）冻结为不可变文件视图，经二次脱敏扫描后由 `mindie_knowledge.community` 提交至贡献者 Fork 并维护上游 PR。 | 打包、提交、PR 标题/正文生成与远程对账均为纯确定性模板，零模型调用。 |

---

## 快速开始与命令行参考

### 环境要求与安装

需要 Python 3.11+（具备支持模块的 SQLite 运行时）与 Git。

```sh
# 安装运行时包
python -m pip install -e .

# 安装含测试套件的开发环境
python -m pip install -e '.[test]'
```

### 领域服务与生命周期 CLI（`mindie-knowledge`）

`mindie-knowledge`（等价于 `python -m mindie_knowledge`）提供有界本地操作，所有子命令均通过 `--config <domain.json>` 指定领域引擎配置：

```sh
# 在前台启动领域知识服务（持有 consumer.lock 与本地认证 RPC）
mindie-knowledge serve --config domain.json

# 查询运行中服务或本地存储的只读状态（含模型尝试计数与已知 token 统计）
mindie-knowledge status --config domain.json

# 查询社区共享开关状态、本地条目数、采集摘要、覆盖缺口、投票与发件箱状态
mindie-knowledge sharing-status --config domain.json

# 导出本地有界故障诊断快照（可选指定 --session 过滤会话诊断标识）
mindie-knowledge diagnostic-status --config domain.json
mindie-knowledge diagnostic-status --config domain.json --session <session-id>

# 执行一次独立的无模型公开知识源同步（共享关闭时同样可用；--resume 可跳过传输退避立即重查）
mindie-knowledge sync --config domain.json
mindie-knowledge sync --config domain.json --resume

# 接收 Harness Stop 事件信封（从 stdin 读取 JSON）并触发异步后台唤醒
mindie-knowledge hook --config domain.json

# 停止当前运行中的领域知识服务
mindie-knowledge stop --config domain.json
```

### 贡献批次恢复与维护命令

当社区贡献批次处于待定、失败或已确认状态时，可通过以下确定性（零模型调用）命令检查或处理指定批次：

```sh
# 只读检查指定批次在循环发件箱与社区账本中的状态记录
mindie-knowledge contribution-inspect --config domain.json --batch <batch-id>

# 对状态未知（unknown）或待核实的批次执行一次有界只读远程 PR 对账，并同步更新两处账本
mindie-knowledge contribution-reconcile --config domain.json --batch <batch-id>

# 显式重发已确认为 failed 或 rejected 的冻结批次载荷（拒绝重放结果未知的写入）
mindie-knowledge contribution-retry --config domain.json --batch <batch-id>

# 清理已确认发布批次的私有暂存文件（保留本地候选直至相同修订进入公开订阅源）
mindie-knowledge contribution-compact --config domain.json --batch <batch-id>
```

### 社区发布、公开仓库校验与隐私脱敏工具

除主命令外，包内还提供面向贡献者提交、内容仓库 CI 校验与源码级脱敏检查的独立模块入口：

```sh
# 贡献者侧直接提交或只读对账单个批次（输出单行 JSON 回执）
python -m mindie_knowledge.community submit     --settings community.json --state-dir ~/.mindie/community --batch batch.json
python -m mindie_knowledge.community reconcile     --settings community.json --state-dir ~/.mindie/community --batch-id <batch-id>

# 内容仓库 CI 校验器：流式校验不可变 Git 提交中的任务包、反馈、契约哈希与隐私合规性
python -I -m mindie_knowledge.publication_check     --repo <checkout> --revision <head-40-sha>     --domain vllm-ascend --base-revision <base-40-sha>     --contract-sha256 <expected-contract-sha256>

# 确定性隐私脱敏扫描器（--check 模式下发现敏感信息返回非零退出码；--profile 输出当前规则集版本）
python -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills
python -m mindie_knowledge.redact --profile
```

### Harness 适配层本地 RPC 与共享接口

各原生 Harness 适配器（如 [Codex 插件](https://github.com/mindie-agent/mindie-agent-codex)、Claude Code、Kimi）负责原生 MCP 派发与会话身份核验，并将请求转发至本组件的本地认证回环接口：

- **`knowledge_query(query, limit?, conditions?, continuation?)`**：检索当前可见分块并按单源引用链聚合结果（`limit` 为 1–20 组），返回块级只读引用 `ref`、当前任务 `task_ref`、观测修订 `feedback_ref`、原文摘录及 `related_next` 翻页令牌。
- **`knowledge_explain(ref)`**：支持两类完整规范引用：
  - `mindie://DOMAIN/TASK`：返回当前任务的可证伪导航摘要、`current_revision`、`block_count` 与 `first_block_ref`（不拼接全量正文）；
  - `mindie://DOMAIN/TASK/blocks/<block-id>@<file-sha256>`：校验当前清单成员哈希后返回单个分块完整内容及前后相邻块引用（`previous_block_ref` / `next_block_ref`）。
- **`knowledge_feedback(ref, rating, reason?)`**：针对 `mindie://DOMAIN/<task-id>@<observed-revision>` 记录 `up` 或 `down` 反馈及可选原因（最多 1000 字符），拒绝绑定到不含观测修订的读取引用。
- **`mindie_knowledge.loop.history_import.import_transcript`**：供 Harness 显式历史导入入口调用，对用户明确选定的历史会话快照执行与实时采集相同的脱敏分块与全量准入门禁。
- **`mindie_knowledge.releases`**：解析插件正式 Release 资产 `mindie-plugin-release.json` 与 `mindie-harness-compatibility/1` 接口注册表，供更新器按当前 Harness 版本选择最高兼容正式版。

---

## 隐私、安全与运行边界保证

1. **单次显式授权与默认零采集**：安装后首次进入原生入口（Kimi/Claude Code 中的 `/mindie-agent` 或 Codex 中的 `$mindie-agent`）仅询问一次贡献意愿并持久化至 `mindie-consent/1`。默认关闭（`enabled=false`）；关闭状态下不执行任何后台采集、文本提取、脱敏扫描或摘要模型调用，而只读检索、插件更新与公开知识同步保持可用。
2. **每次外发与模型调用前重验权威配置**：在读取对话记录、启动摘要进程以及执行任何外部写入前，核心均重新读取并校验共享配置文件与绝对路径指向的 `mindie-consent/1`。配置缺失、损坏或权限不匹配立即闭锁写路径并按真实故障上报，绝不伪装成首次安装引导。
3. **只采公开可见消息，严禁模型改写正文**：仅提取经授权的用户输入、助手可见进展与最终回答；工具调用/返回、隐藏思维链（reasoning）、系统提示词注入与非当前任务历史一律排除。分块正文完全由无损切分与确定性脱敏生成，摘要模型（LangMem）只能生成元数据索引，无法修改分块正文字节。
4. **内容与代码严格隔离**：每个公开知识源均声明 `publication-contract.json`。同步与发布在推进前均校验契约及其 SHA-256；校验器与客户端仅将候选内容作为不可变 Git blob 读取，绝不从内容 PR 或知识仓库加载、导入或执行任何 Python 代码。
5. **发布状态边界与持久性契约（`state-v4`）**：`mindie_knowledge.state_layout` 声明持久化 `FORMAT`（当前为 `4`，目录 `state-v4`）与产品状态边界 `RELEASE_VERSION = "0.0.1"`。一旦正式版本打开状态目录，兼容升级直接复用已有材料、游标、模型尝试记录与发布回执；缺失或损坏的布局元数据直接报错，绝不静默重置用户数据。

---

## 文档导航

完整技术契约与平台实现说明位于 [`docs/`](docs/README.md) 目录（提供中英文双语版本）：

| 文档主题 | 中文文档 | English Document | 核心内容概述 |
|---|---|---|---|
| **文档目录索引** | [docs/README.md](docs/README.md) | [docs/README.en.md](docs/README.en.md) | 全部架构、协议、平台与发布契约文档导览 |
| **领域闭环运行时契约** | [mindie-loop.zh.md](docs/mindie-loop.zh.md) | [mindie-loop.md](docs/mindie-loop.md) | 授权门禁、公开对话采集、任务包格式、BM25 检索、引用分组、反馈与同步完整规范 |
| **公开对话材料与增量索引** | [public-transcript-capture.zh.md](docs/public-transcript-capture.zh.md) | [public-transcript-capture.md](docs/public-transcript-capture.md) | `public-transcript` 管道、Gitleaks 脱敏、LangMem 增量索引账本与生命周期所有权 |
| **显式历史会话导入** | [history-import.zh.md](docs/history-import.zh.md) | [history-import.md](docs/history-import.md) | `import_transcript` 快照准入、前缀校验、断点续导与远端合并衔接 |
| **社区共享（贡献者侧发布）** | [community-sharing.zh.md](docs/community-sharing.zh.md) | [community-sharing.md](docs/community-sharing.md) | `mindie_knowledge.community` 凭据隔离、批次幂等提交、只读对账与 Skill 包校验辅助 |
| **仓库评审 Bot 契约** | [repository-bot-contract.zh.md](docs/repository-bot-contract.zh.md) | [repository-bot-contract.md](docs/repository-bot-contract.md) | `mindie-content-review/2` 外部维护者 Bot 审核、有限脱敏修正、撤回与 Skill 提案规则 |
| **发布兼容性与文件锁** | [release-compatibility.zh.md](docs/release-compatibility.zh.md) | [release-compatibility.md](docs/release-compatibility.md) | `mindie-plugin-release/1` 版本选择、源码一致性校验与跨平台 `locks` 实现 |
| **服务与尝试进程生命周期** | [service-process-lifetime.zh.md](docs/service-process-lifetime.zh.md) | [service-process-lifetime.md](docs/service-process-lifetime.md) | Windows 计划任务、Linux systemd 254+ 与 macOS 管道守卫的跨平台生命周期控制 |
| **摘要进程与临时目录所有权** | [summary-process-ownership.zh.md](docs/summary-process-ownership.zh.md) | [summary-process-ownership.md](docs/summary-process-ownership.md) | `MINDIE_SUMMARY_SCRATCH` 内部契约、进程树收割顺序与私有临时目录清理 |
| **Windows 知识服务宿主** | [windows-service-host.zh.md](docs/windows-service-host.zh.md) | [windows-service-host.md](docs/windows-service-host.md) | Windows Task Scheduler 宿主、DPAPI 单次上下文传递与无窗口标准流保护 |
| **Windows 进程树所有权** | [windows-process-ownership.zh.md](docs/windows-process-ownership.zh.md) | [windows-process-ownership.md](docs/windows-process-ownership.md) | `spawn_owned` 挂起创建、`KILL_ON_JOB_CLOSE` 作业绑定与管道泄漏回归验证 |
| **框架稳定性与验证边界** | [framework-stability.zh.md](docs/framework-stability.zh.md) | [framework-stability.md](docs/framework-stability.md) | 日常使用恢复语义、Windows 原子重命名配置读写及组件与原生验收边界 |

---

## 开发与验证

从仓库根目录运行测试与隐私扫描：

```sh
# 运行完整单元测试套件（未设置 MINDIE_FRAMEWORK_SOURCE 时自动使用提交的解析器固件）
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python -m pytest -q tests

# 扫描公开文档与内置技能数据，确保不包含任何敏感或私有信息
python -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills
```

> **提示**：若未设置 `MINDIE_FRAMEWORK_SOURCE`，`tests/conftest.py` 默认使用仓库内提交的 `tests/fixtures/production-parsers` 并校验三个解析器文件存在；若显式指定了不存在或不完整的路径，pytest 会立即以环境配置错误退出，而不会回退搜索生产安装目录。可通过 `MINDIE_PARSER_KIMI`、`MINDIE_PARSER_CC`、`MINDIE_PARSER_CODEX` 单独覆盖对应解析器文件。

参与开发与提交 PR 前，请务必阅读 [贡献指南（CONTRIBUTING.md）](CONTRIBUTING.md) 并完成必填的九条设计原则审查。

---

## 许可证

- 代码、脚本与运行时组件采用 [MIT License](LICENSE)。
- 知识语料与文档规范采用 [CC-BY-4.0 License](LICENSE-CORPUS)（详见文件说明）。
