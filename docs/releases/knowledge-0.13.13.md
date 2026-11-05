# mindie-knowledge 0.13.13：RSI 自动 `reuse -> explore` 晋升、候选哈希去重与 C6 沉淀加固

**中文** · [English](https://github.com/mindie-agent/knowledge/blob/v0.13.13/docs/releases/knowledge-0.13.13.en.md)

- **工作区门控首次查询保底 `reuse` 与 `candidate_sha256` 独立证伪晋升（`graded/index.py`）**：
  - 在工作区门控下，该工作区自己的首次 `knowledge_query` 保底进入 `effective_mode="reuse"`，确保 Agent 先获取并尝试 `L1–L3`。保底只认本工作区审计（`logs/knowledge_query_audit.jsonl` 中 `workspace_scope` 一致的 `effective_mode=reuse`）；共享 `profile/` 或其它目录里的 reuse 记录不能代替；
  - `MINDIE_BENCH_WORKSPACE` 只读取该目录的 `logs/experiment-ledger.jsonl`，不会回退到进程 cwd 或其它树的账本。未设置该变量时，仅在 cwd 或同级 `workspace/`、`work/` 下真实存在账本文件时才门控；名为 `workspace` 的空目录不会关掉 `explore`；
  - 统计被证伪记录时按 `candidate_sha256` / `candidate_hash`（否则 `source_digest`）去重。自最近一次进展之后累计 `>= 3` 个不同候选哈希，下一次 `knowledge_query(mode="reuse")` 才自动晋升到 `explore`（`auto_switched_from_reuse=True`）。`mode="diagnose"` 保持 diagnose，不会改写成 explore；
  - 审计记录 `requested_mode`、`effective_mode`、`auto_switched_from_reuse`、`caller_initiated_explore`、`observed_candidate_hashes`（计入晋升的证伪哈希）、`distinct_candidate_hashes`、`workspace_scope` 与 `explore_disabled`。
- **`MINDIE_DISABLE_RSI_EXPLORE=1` 物理级 `L4` 隔离（`graded/index.py`、`graded/closure.py`）**：
  - 强制 `effective_mode="reuse"`。检索结果和继承前提不返回 L4 正文或 L4 id，`knowledge_explain` 的闭包同样不展开 L4 祖先；调用方点名的 L4 ref 只回 `not_found`，不附正文。不填写 `held_back_l4_ids`。
- **C6 继续要求已验证的消融收据（`graded/distill.py`）**：
  - `logs/experiment-ledger.jsonl` 里的 `full_candidate` / `ablate_*` 原始行不会被合成 `c4_verification` 收据，也不会把缺少消融收据的 L4 提案改写成 L2 后提交。L4 仍然必须绑定通过校验的 `mindie-bench-ablation/1` 或 `/2` 收据，否则拒绝，不写知识库。
- **单条损坏材料隔离（`#113`）**：隔离检索过程中单条不可读材料异常，避免中断整体知识查询响应。
- **Ascend 950DT (A5) 16 组端到端 RSI A/B 基准验证（`track-RSI-E2E-v4`）**：
  - 在双宿主（Codex `gpt-6.1-sol xhigh` 与 DSH `deepseek-flash max`）× 4 个受控任务 Fixture 上完成单一变量控制 A/B 评测：对照组 `G2` 通过率 `0/8 (0.0%)`，实验组 `G4` 通过率 `8/8 (100.0%)`，`8/8` 触发零人工干预的 `reuse -> explore` 自动晋升（`caller_initiated_explore=False`，独立证伪候选哈希 `>= 3`），`8/8` 完成 Stop 阶段 `graded-c6` 本地 Git 沉淀（`completed:applied`），并通过 Codex `gpt-6-astra (max)` 独立可信度审计。
