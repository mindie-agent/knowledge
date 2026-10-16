# MindIE 领域闭环运行时契约

中文 | [English](mindie-loop.md) · [返回文档索引](README.md)

本文定义 `mindie-knowledge` 的领域闭环运行时协议，涵盖双层语料存储、确定性公开对话采集、增量分块索引、公共知识同步、社区外发与独立有用性反馈。

## 1. 核心架构与数据分层

`mindie-knowledge` 将数据严格划分为两层：
- **公共领域层（`public/`）**：从上游公开 Git 仓库（如 `mindie-agent/knowledge-vllm-ascend`）只读同步的结构化知识包，受 `publication-contract.json`（含 SHA-256 摘要固定）约束。
- **本地私有与草稿层（`local/`）**：当前用户经显式授权项目范围准入的任务对话分块（`materials/`）、任务清单（`PACKAGE.md`）与 SQLite 账本（`state-v4.sqlite3`）。

权威正文字节始终存储为普通 Markdown 文件；SQLite 仅保存紧凑的元数据头、游标、队列状态与外部操作回执；ReMe 在进程内提供可随时从权威 Markdown 重建的 BM25 与文件引用检索索引。

## 2. 公开知识同步（`sync`）

- 客户端流式拉取并校验远端知识包清单（`mindie-material-task/1`、`mindie-entry/3` 与 `mindie-material-block/1`），拒绝任何超出允许路径（`tasks/<64位哈希>/PACKAGE.md` 与 `materials/<4位序号>.md`）或未通过 `mindie_knowledge.redact` 校验的内容。
- 整个候选快照完整校验通过后才原子切换活动视图；同步过程零模型调用，失败时完整保留上一版可用语料库。

## 3. 经验采集与增量索引（Capture & Indexing）

- **显式授权前置**：仅当用户已配置并启用社区贡献且当前任务属于已授权项目范围时，Stop Hook 才将任务增量持久准入至本地队列。未配置、显式停用、范围不符与运行故障严格区分。
- **确定性投影与闭锁式脱敏**：宿主适配器仅提取用户输入、助手公开进展与最终回答；在落盘、送入模型或外发前执行 Gitleaks 与 `mindie_knowledge.redact`（规则集 `r3`）校验。
- **轻量导航生成**：适配器调用宿主原生小模型（配合 LangMem）仅为新增分块生成标题、检索摘要与任务短导航，绝不改写正文字节。

## 4. 确定性发件箱与只读对账（Outbox & Reconciliation）

- 已完成索引的本地任务包与可发布反馈被合并为 `mindie-contribution/1` 批次并提交 GitHub PR。
- 若网络中断导致远端推送或 PR 创建结果未知（`unknown`），运行时保存原操作身份与幂等键，在退避窗口后仅通过只读查询与远端分支/PR 对账，绝不盲目重放写操作或重复调用付费模型。

## 5. 独立有用性反馈（Feedback）

- 消费方任务在参考某条知识或经验后，可针对精确观测修订（`mindie://DOMAIN/<task-id>@<observed-revision>`）记录可选的 `up` / `down` 反馈与单行说明。
- 反馈作为独立记录存储与外发，不设裁判模型，也不将投票数当作真理性认证。
