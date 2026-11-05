# mindie-knowledge 0.13.13：RSI 自动 `reuse -> explore` 晋升、候选哈希去重与 C6 沉淀加固

**中文** · [English](https://github.com/mindie-agent/knowledge/blob/v0.13.13/docs/releases/knowledge-0.13.13.en.md)

- **工作区门控首次查询保底 `reuse` 与 `candidate_sha256` 独立证伪晋升（`graded/index.py`）**：
  - 在 `workspace_gated` 模式下，同一工作区的首次 `knowledge_query` 保底进入 `effective_mode="reuse"`，确保 Agent 先获取并尝试 `L1–L3` 常规知识单元；
  - 统计工作区账本（`logs/experiment-ledger.jsonl`）中的被证伪（`verdict="refuted"`）实验记录时，优先按独立候选源码摘要（`candidate_sha256` / `candidate_hash`）去重计数，防止重复写入同一候选修改虚增失败次数；当累计满 `>= 3` 个不同候选哈希后，下一次 `knowledge_query(mode="reuse")` 自动晋升至 `effective_mode="explore"`（`auto_switched_from_reuse=True`）并挂载 `L4` Mutator 契约；
  - 查询审计日志（`knowledge_query_audit.jsonl`）显式记录 `requested_mode`、`effective_mode`、`auto_switched_from_reuse`、`caller_initiated_explore`、`observed_candidate_hashes` 与 `explore_disabled`，严格区分引擎自动晋升与调用方显式请求。
- **`MINDIE_DISABLE_RSI_EXPLORE=1` 物理级 `L4` 隔离（`graded/index.py`）**：
  - 当设置 `MINDIE_DISABLE_RSI_EXPLORE=1`（如 A/B 对照组 `G2`）时，强制锁定 `effective_mode="reuse"`，物理剥离全部 `L4` 单元并清空 `held_back_l4_ids` 元数据，杜绝任何 `L4` 信息泄露。
- **非 C4 结构化 `L4` 消融证据归一化与 C6 蒸馏准入（`graded/distill.py`）**：
  - 支持将 `logs/experiment-ledger.jsonl` 中的 `full_candidate` 及留一法（`ablate_*`）实验账本自动归一化为 `c4_verification` 消融收据，打通 Stop 阶段 `graded-c6` 知识提炼与本地裸 Git 仓库沉淀闭环。
- **单条损坏材料隔离（`#113`）**：隔离检索过程中单条不可读材料异常，避免中断整体知识查询响应。
- **Ascend 950DT (A5) 16 组端到端 RSI A/B 基准验证（`track-RSI-E2E-v4`）**：
  - 在双宿主（Codex `gpt-6.1-sol xhigh` 与 DSH `deepseek-flash max`）× 4 个受控任务 Fixture 上完成单一变量控制 A/B 评测：对照组 `G2` 通过率 `0/8 (0.0%)`，实验组 `G4` 通过率 `8/8 (100.0%)`，`8/8` 触发零人工干预的 `reuse -> explore` 自动晋升（`caller_initiated_explore=False`，独立证伪候选哈希 `>= 3`），`8/8` 完成 Stop 阶段 `graded-c6` 本地 Git 沉淀（`completed:applied`），并通过 Codex `gpt-6-astra (max)` 独立可信度审计。
