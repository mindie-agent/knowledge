# mindie-knowledge 0.13.17：权威状态契约违规诊断与 Diagnostics 2.0 升级

[English](knowledge-0.13.17.en.md)

- **权威 SQLite 状态契约违规观测（`owned_state.py` + `loop/dfx.py`）**：在 `open_database` 与 `require_columns` 检测到权威状态库符号链接、残留 WAL/SHM/记录但主库缺失、0 字节损坏或必需列缺失并按既有契约立即失败阻断时，同步通过 `record_contract_violation` 记录结构化 `owned_sqlite_authority` 契约违规事件（携带 `reason_code` 与精确源码根目录，零本地路径或自由文本泄露）。
- **服务崩溃分类与 Diagnostics 2.0 对齐**：升级 `mindie-diagnostics` 依赖至正式版 `v0.5.11`（`171623584aa812677df907518dcb55b06493241b`），在 `loop/dfx.py` 中透传精确 `source_root`、`reason_code`、`substage` 与 `contract_name`，支持主仓集中归档与精确提交行号定位。
