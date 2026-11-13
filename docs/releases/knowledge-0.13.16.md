# mindie-knowledge 0.13.16：SQLite 初始化锁下限、租约读取端到端预算与历史文件状态诚实性闭环

[English](knowledge-0.13.16.en.md)

- **SQLite 统一 5s 锁策略、宿主 Hook 截止时间对齐与多域注销保底预算（`mindie-agent#332` §1 闭环）**：`open_database` 与 `accept_stop` 统一采用 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000`（`SQLITE_BUSY_FLOOR_SECONDS = 5.0`）初始化策略，移除热路径动态 `1s~3s` `busy_timeout` 覆写；将初始化文件锁（`StartLock`）、SQLite 建连/WAL 切换与租约查询统一收敛至单一单调时钟截止时间（`deadline`），对剩余预算低于 `5.0s` 锁下限的调用在进入阻塞锁或 SQLite 之前立即失败返回（`budget-exhausted` / `AdmissionUnavailable`），在保持默认 `STOP_TIMEOUT_SECONDS = 6.0` 与 `deactivate_registered_domains(..., timeout=6.0)` 严格受控于宿主插件 `8.0s`/`10.0s` Hook 外层预算的前提下防止 `Admission` 与 `Store` 叠加等待，并为 5+ 多域注销按剩余唯一库保底分配 `>= 5.5s` 可用单库预算。
- **路由与实验账本文件状态诚实性（Python 3.14 兼容）**：`_existing_routing_document`、`read_bounded_jsonl` 与 `resolve_workspace_ledger` 改用显式 `os.lstat` + 常规文件校验替代 `Path.is_file()`，严格仅在 `FileNotFoundError` 时视为空缺；目录、非常规文件或权限/I/O 异常（含 Python 3.14 下 `is_file()` 静默吞除 `OSError` 的场景）均显式报错阻断，杜绝损坏或无权限历史被误判为“首次空历史”。
- **知识仓交付物契约测试结构化对齐**：`tests/test_corpus_deliverables.py` 将对知识单元正文的英文关键词匹配升级为结构化 JSON 消融回执字段校验（`candidate_id`、`leave_one_out_arms`、`fallback_boundary`），并支持 `<owner>/<repo>@<sha>:<path>` 规范代码仓证据定位符。
