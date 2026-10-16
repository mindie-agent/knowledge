# 内容仓库审核机器人契约（`mindie-content-review/2`）

中文 | [English](repository-bot-contract.md) · [返回文档索引](README.md)

本文定义公开知识内容仓库（如 `mindie-agent/knowledge-vllm-ascend`）的自动化校验与审核机器人契约 `mindie-content-review/2`。

## 1. 契约声明与版本固定

- 内容仓库根目录下的 `publication-contract.json` 声明当前格式版本、允许的目录结构、脱敏规则集版本（`r3`）与机器人审核契约标识。
- 客户端同步（`sync`）与外发（`share-outbox`）均校验 `publication-contract.json` 的精确内容摘要，防止未受控的格式变更破坏客户端解析。

## 2. 确定性门禁与模型合规审核

- **确定性格式与脱敏检查**：校验 PR 变更范围、任务知识包 Schema、分块哈希一致性、PR 标题/正文及全部文件块的 `mindie_knowledge.redact` 与 Gitleaks 扫描。
- **Grok Bot 合规审核**：对新增或更新的知识包执行敏感信息与合规性审查，通过后自动合入，发现违规或冲突时拒绝合入并给出明确原因。
- **区分内容贡献与工程变更**：仅修改 `tasks/` 下知识包的 PR 按内容发布流程自动审核；涉及仓库配置、工作流或文档的变更归类为 `development` 模式，交由维护者人工合入。
