# 参与贡献（Contributing to `mindie-knowledge`）

**中文** | [English](CONTRIBUTING.en.md) · [返回仓库首页](README.md)

欢迎参与 **MindIE Knowledge**（`mindie-agent/knowledge`）的开发与改进！本仓库提供 MindIE Agent 的领域知识同步、本地 BM25 检索、双层隐私脱敏与社区经验外发运行时。

为了让代码库始终保持高可维护性、零隐私泄漏和清晰的故障可观测性，提交 Pull Request 前请了解以下开发约定。

---

## 1. 核心工程约定（为什么这么设计？）

1. **知识仅供参考，不喧宾夺主**：
   检索命中、小模型导航摘要、有用性投票或 PR 合入都**不代表对某条经验的权威认证**。请保持“公开知识包”、“本地任务草稿”、“实际业务使用”与“独立反馈”之间的清晰边界。
2. **严守双层闭锁式脱敏（Fail-Closed Redaction）**：
   任何涉及对话采集、索引、外发或同步的改动，都必须确保未脱敏原文、工具调用噪音、隐藏推理链、内网地址、`<user>@<host>` 登录目标、个人目录路径或凭据绝不会进入公开分发路径。
3. **禁止静默降级或隐藏错误**：
   当读取失败、配置损坏、脱敏扫描器异常或子进程超时发生时，必须向调用者如实报告失败阶段与原因——绝不能把读取失败伪装成“无匹配结果”，不能把配置损坏伪装成“首次安装引导”，更不能对结果不确定（`unknown`）的外部写入或付费模型调用做盲目自动重试。
4. **保持简洁统一命名**：
   当前 CLI 入口为 `mindie-knowledge`（包名 `mindie-knowledge`，模块 `mindie_knowledge`），请勿引入已废弃的旧别名或第二套状态库。
5. **区分固定夹具测试与实机验收证据**：
   仓库内的单元测试使用确定性夹具验证协议与异常分支；涉及真实大模型、客户端宿主或昇腾 NPU 的结论需基于可追溯的实机验证记录，不虚构验收证据。

---

## 2. 本地开发环境与必跑检查

在提交代码或文档前，请在仓库根目录下运行以下两条验证命令：

```bash
# 1. 安装可编辑运行时与测试依赖
python3 -m pip install -e '.[test]'

# 2. 运行单元与集成测试套件
MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python3 -m pytest -q tests

# 3. 运行确定性隐私脱敏扫描，确保公开文档与技能文件零敏感信息命中
python3 -m mindie_knowledge.redact --check README.md README.en.md CONTRIBUTING.md CONTRIBUTING.en.md docs mindie_knowledge/skills
```

> [!TIP]
> **编写文档与测试数据时的防踩坑提示**：
> - `mindie_knowledge.redact` 对公开文档同样严格执行扫描。在举例说明路径、机器名或登录串时，请使用带尖括号的占位符（如 `/home/<user>/workspace`、`C:\Users\<user>\...`、`<user>@<host>`），或使用 RFC 5737 文档专用网段（`192.0.2.x`）。
> - 当未设置 `MINDIE_FRAMEWORK_SOURCE` 时，测试套件会自动使用仓库内的 `tests/fixtures/production-parsers`；若显式传入了不存在的路径，测试会直接报错退出，而不会静默回退到系统目录。

---

## 3. 提交 PR 前的设计原则自检

在创建或更新开发 PR（包括代码、配置、依赖、CI 与文档改动）前，请先阅读 [MindIE Agent 工程设计原则](https://github.com/mindie-agent/mindie-agent/blob/main/docs/design-principles.md)，并在 PR 描述中按模板（[`.github/pull_request_template.md`](.github/pull_request_template.md)）简要填写 `## 设计原则 Review`：

- **这次改动降低了什么实际成本？** 是否引入了不必要的常驻开销、重复计算或额外心智负担？
- **组件边界是否清晰？** 是否复用了宿主与操作系统的成熟机制，并在替代旧实现后清理了重复状态？
- **所有受影响的失败路径是否对调用者可见？** 是否区分了“已完成”、“失败”与“外部副作用不确定（`unknown`）”？
- **实际运行了哪些验证命令？** 哪些外部环境边界尚未在本次变更中验证？
