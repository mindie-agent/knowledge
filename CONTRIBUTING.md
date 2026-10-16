# 贡献指南（Contributing）

中文 | [English](CONTRIBUTING.en.md)

感谢关注并参与 **MindIE Knowledge**（`mindie-agent/knowledge`）的开发与维护。本仓库承载 MindIE Agent 的共享领域知识与任务经验闭环运行时。

提交代码、配置、构建/CI 或文档变更前，请遵循以下工程边界与验证要求。

---

## 核心工程契约

1. **遵循领域闭环契约**：所有改动必须符合 [领域闭环运行时契约](docs/mindie-loop.zh.md)（[English](docs/mindie-loop.md)）。
2. **严格区分概念边界**：保持**知识（knowledge）**、**任务经验（experience）**、**实际业务使用（actual use）**与**独立反馈（independent feedback）**的边界清晰分离。检索命中、摘要生成、投票或合入均不赋予条目决策权威。
3. **坚守隐私脱敏防线**：任何涉及公开发布、同步或社区共享的变更必须保持闭锁式（fail-closed）脱敏边界，严禁将原始会话记录、未脱敏正文、本地私有路径、凭据或内部使用证据带入公共分发路径。
4. **保持当前入口与命名**：当前命令行入口为 `mindie-knowledge`（包名 `mindie-knowledge`，模块 `mindie_knowledge`）。请勿恢复已退役的旧工作区安装方式或遗留别名。
5. **保持实现状态与真实验收证据分离**：固定夹具（fixture）测试仅验证协议与组件行为；任何关于真实模型、原生宿主或昇腾 NPU 的结论必须具备可追溯的实际验收记录。

---

## 本地开发与必做检查

在仓库根目录下安装测试依赖并执行完整检查：

```sh
# 1. 安装可编辑包与测试依赖
python -m pip install -e '.[test]'

# 2. 运行单元与集成测试套件
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python -m pytest -q tests

# 3. 扫描所有公开文档与技能文件，确保零敏感数据命中
python -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills
```

- 当 `MINDIE_FRAMEWORK_SOURCE` 未设置时，测试套件默认使用仓库内提交的 `tests/fixtures/production-parsers` 并校验三个解析器文件存在；若显式传入了错误的路径，测试会作为配置错误直接失败退出，不会静默回退到系统安装目录。
- 若改动涉及社区发布（`mindie_knowledge/community`）或公开知识仓库校验（`mindie_knowledge/publication_check.py`），请确保对应的格式、契约与传输测试全部通过。
- 请勿在任何 Markdown 文档、测试固定数据或 PR 描述中写入类似内网 IP、SSH 登录目标、包含真实用户名的本地目录路径、访问令牌或内部邮箱的字符串。

---

## 提交 PR 前必做的设计原则审查（Mandatory Design-Principles Review）

在创建或更新任何开发 PR（包括 Draft PR，涵盖代码、配置、依赖、CI 与项目文档变更）之前，贡献者**必须**阅读最新的 [MindIE Agent 九条设计原则](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md)，并对照最终 diff 与受影响行为逐条完成审查：

1. **以完成用户实际目标的总成本衡量 Agent 效率**
2. **只将边界明确的封闭问题工具化**
3. **不能增加 Agent 的理解负担**
4. **每个工具有明确的边界和定义**
5. **Skill 主要提供信息**
6. **知识仅供参考**
7. **所有工具和 Skill 按需介入**
8. **优先复用成熟机制与已有有效成果**
9. **工具可观察、可定位，禁止隐藏错误**

### 审查与 PR 描述要求

- **错误必须对调用者可见**：严禁将必要步骤失败转化为成功、空结果、首次使用引导、用户停用或未记录的降级行为。必须明确区分已完成工作、失败与结果不确定的外部副作用；不得自动重放结果不确定的外部写入，也不得用后续清理异常掩盖主错误。
- **提交前修正违规项**：审查中发现违反设计原则的问题，必须在提交 PR 前修复并复查。
- **在 PR 描述中完整记录审查结论**：按照 [`.github/pull_request_template.md`](.github/pull_request_template.md) 填写 `## 设计原则 Review` 章节，明确记录：
  - 审查覆盖的提交哈希（commit SHA）与设计原则文档版本（revision）；
  - 九条原则的具体审查结论与适用性依据（不适用项需结合本次 diff 说明具体原因，不得使用笼统的 `N/A` 或“已遵循全部原则”代替）；
  - 发现的问题与修正措施；
  - 实际运行的验证命令及尚未覆盖的外部环境边界。
