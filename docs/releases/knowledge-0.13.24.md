# mindie-knowledge 0.13.24：`local-only` 同意层、采集与外发权限分离、匿名 HTTPS 公共同步与密码脱敏补强

[English](mindie-knowledge-0.13.24.en.md)

`mindie-knowledge v0.13.24` 将 `local-only` 提升为一等同意选项，并把“本地采集”与“GitHub 外发”拆成两条独立授权链：

- **`local-only` 一等同意层（`mindie-consent/1`）**：`CHOICES` 新增 `local-only`，仍由 `consent_store.record_choice` 唯一写入并保持单调 `choice_revision`。`activate_reporting` 仅在保存的 `contribute + choice_revision` 下启用诊断外发；`local-only` 写入禁用报告策略、返回 `status="disabled"` 且绝不启动 worker。
- **采集授权与外发授权彻底分离（`allows_capture` / `allows_publication`）**：`loop/settings.py` 新增 `effective_tier`、`allows_publication` 与 `publication_block_kind`；`PublicationWorker`、引擎发件箱、`community.settings.live_gate`、`community.publish` 与只读对账全部改用外发授权。`local-only`（或 `local_only=true`）下即使磁盘残留旧 `fork/account` 字段，也不会构造传输、调用 `gh`、检查 fork、生成 outbox 批次或创建 PR。
- **`local-only` → `contribute` 零补发溯源边界**：采集、材料与本地蒸馏的单元以附加元数据记录 `origin_tier`（不改变 `state-v4.sqlite3`、`FORMAT = 1`、`MaterialStore` `DATA_FORMAT_V1` 与既有读取器）；带 `origin_tier=local-only` 的材料与分级单元永久不进入后续 `contribute` 导出。
- **纯匿名 HTTPS 公共语料同步**：公开规范仓库（`https://github.com/mindie-agent/knowledge-*`）的 `clone`/`fetch` 附带 `GIT_TERMINAL_PROMPT=0` 与 `-c credential.helper= -c core.askPass= -c http.extraHeader=`，在宿主 `gh` 缺失或凭据助手损坏时仍可匿名拉取；`HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY`/`SSL_CERT_FILE`/`GIT_SSL_CAINFO` 等企业代理与 CA 配置保持不变。
- **首次落盘前的密码脱敏补强（`r4`）**：新增任意长度（>= 1 字符）`password`/`passphrase`/`ssh_password` 赋值规则（含 JSON 转义双引号写法）、CLI 密码参数规则，以及中文密码提示句（密码标签后接“是/为/冒号/空白”再跟值）规则；仅替换秘密值本身并保留结构，文档中的“密码输入”“密码学”“密码回退”等措辞零误报。
