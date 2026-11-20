# mindie-knowledge 0.13.25：只读服务开箱即用、蒸馏快照落盘前脱敏与 Windows 双层无窗口 PE 校验

**中文** | [English](knowledge-0.13.25.en.md) · [返回文档中心](../README.md)

`mindie-knowledge v0.13.25` 打通了全新环境与只读模式下的零门槛冷启动体验，补齐了 `knowledge_distill` 空证据调用时关联快照落盘前的闭锁脱敏链路（同时保持与 `v0.13.24` 100% 状态回滚兼容），并在 Windows 边界补齐了 `pyvenv.cfg` 双层 `pythonw.exe` 无窗口 PE 子系统核验。

---

## 1. 只读服务开箱即用与 `capture_mode="disabled"` 配置解耦

在尚未安装 Gitleaks 扫描器、宿主 `transcript_adapter` 尚未落盘，或候选版本自检未配置 `admission_path` 的场景下，公共知识读取与同步现在可以直接冷启动运行，同时在真正进入会话采集与经验蒸馏时继续执行严格闭锁校验：

- **结构化配置校验与只读冷启动（`mindie_knowledge/loop/cli.py`、`mindie_knowledge/loop/engine.py`、`mindie_knowledge/loop/transport.py`）**：`validate_config` / `config_at`、`Engine`、`_serve()` 与 `Service._identify` 原生支持 `capture_mode="disabled"`，并允许缺省或为 `None` 的 `redactor_executable`、`transcript_adapter` 与 `admission_path`（非 `None` 但为相对路径或非 `.py` 后缀的非法配置仍立即拒绝）。在只读模式下即可直接完成 `knowledge_query`、`knowledge_explain`、本地 `knowledge_feedback`（`publishable=False`）与 `knowledge_sync`，全程零 `admission.sqlite3` 创建。
- **配置结构校验与同意书状态解耦（`mindie_knowledge/loop/domain_catalog.py`）**：`validate_config` 仅针对配置字典做纯结构校验，不耦合实时同意书状态，确保用户在将同意书从 `read-only` 切换为 `local-only` 或 `contribute` 后，`domain_catalog.prepare` 与 `domain_catalog.retire_changed` 仍能平滑读取并升级既有只读域路由；`_serve()` 对尚未就绪的 `.py` `transcript_adapter` 通过 `transcript_adapter_path` 按需延迟加载。
- **采集与蒸馏入口严格闭锁（`mindie_knowledge/loop/handoff.py`、`mindie_knowledge/loop/transcript_increment.py`）**：`Stop` 准入与唤醒（`accept_stop`、`request_wake`）、后台增量采集（`Engine._process_capture`）及 `knowledge_distill` 强制执行 `validate_capture_config`，校验活跃采集租约、绝对路径 `.py` `transcript_adapter` 以及已安装的绝对路径 `redactor_executable`（`gitleaks`）。当同意书关闭采集时，`Stop` 直接返回 `stage="inert"`（`reason="sharing-disabled"`，零故障上报、零 SQLite 写入）；当同意书开启采集但缺少脱敏器或解析器配置时，显式返回 `stage="unavailable"`（`reason="configuration"`）。

---

## 2. 蒸馏关联快照落盘前闭锁脱敏与回滚兼容（`graded:related-meta`）

针对 `knowledge_distill` 调用方通过 `topic` 或 `target_signature` 传入作用域标识（包括 `inputs == []` 的空证据调用），确保任何未脱敏字符串在通过扫描器校验前绝不写入 SQLite：

- **空输入作用域闭锁扫描（`mindie_knowledge/graded/distill.py`）**：`prepare_request` 的脱敏条件扩展为 `selected or history or scope`，即使 `inputs == []`，只要调用方提供了 `topic` 或 `target_signature`，也会先经 `redact_tree`（Gitleaks + `r4` + 私有路径掩码）完成闭锁清洗；扫描器缺失或不可用时在任何状态落盘前直接失败。
- **先脱敏后持久化与双键回滚兼容（`mindie_knowledge/graded/worker.py`）**：`worker.run` 先在内存中解析候选关联单元与被拒历史（`_load_related_snapshot`），完成 `prepare_request` 脱敏后再开启写事务将清洗后的 `related_units`、`rejected_history` 与 `_history_scope` 写入 `graded:related:<hash>`；同时将脱敏前的 `history_scope_digest` 与合并后的 `redaction_rules` 独立存入伴生键 `graded:related-meta:<hash>`，使 `graded:related:<hash>` 结构与 `v0.13.24` 保持 100% 回滚兼容，并在缓存命中与并发写入重算时精确保持同一 `request_digest`。

---

## 3. Windows 双层 `pyvenv.cfg` 无窗口 PE GUI 校验加固

Windows 虚拟环境下的 `Scripts\pythonw.exe` 启动器本身虽为 GUI 子系统（`Subsystem == 2`），但运行时会转发调用 `pyvenv.cfg` 中 `home` 指向的基础解释器 `home\pythonw.exe`；若基础解释器缺失或被替换为控制台子系统（`Subsystem == 3`），仍会弹出黑色控制台窗口：

- **目标启动器 + 基础解释器双层 PE 核验（`mindie_knowledge/windows_interpreter.py`）**：`require_windowless(python)` 对目标 `pythonw.exe` 及 `pyvenv.cfg` 声明的 `home\pythonw.exe` 均执行 `_verify_pe_gui` 校验，核验 `MZ` 头、有界偏移 `64 <= e_lfanew <= 16 MiB`、`PE\0\0` 签名、`PE32`/`PE32+`（`0x10B`/`0x20B`）与 `IMAGE_OPTIONAL_HEADER.Subsystem == 2`（`IMAGE_SUBSYSTEM_WINDOWS_GUI`）。
- **对齐 CPython 3.13 启动器查找优先级**：按同级 `target.parent / "pyvenv.cfg"` 优先于父级虚拟环境根目录 `target.parent.parent / "pyvenv.cfg"` 的顺序解析 `home`（与 CPython 3.13 `PC/venvlauncher.c` 及 `mindie-harness` §2.4 完全一致）；缺失、损坏或控制台子系统一律以 `ServiceHostError(stage="interpreter", code="windowless_interpreter_missing" | "windowless_interpreter_invalid")` 闭锁拒绝，且 `TimeoutError` 原样上抛。

---

## 4. 验证结果

- **全量隔离测试套件**：`3,969 passed`（宿主机环境变量完全沙箱隔离，排除 `live_npu` 标记）。
- **R2 专项契约套件**：`82/82 passed`，覆盖真实扫描器支撑的分级蒸馏快照脱敏、只读与 `capture_mode="disabled"` 冷启动闭环、`domain_catalog` 路由升级及 Windows 双层 `pyvenv.cfg` PE 校验（`tests/test_readonly_capture_disabled.py`、`tests/test_windows_interpreter.py` 等）。
- **合规与卫生门禁**：`mindie_knowledge.redact --check`（`r4` 规则集）`0` 命中，Unix LF 行尾检查、`state-service` PR 检查单与跨仓代码/文档卫生门禁全部通过。
