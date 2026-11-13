# mindie-knowledge 0.13.16：SQLite 初始化锁下限、租约读取端到端预算与历史文件状态诚实性闭环

[English](knowledge-0.13.16.en.md)

- **SQLite 初始化锁下限与租约读取端到端预算（`mindie-agent#332` §1 闭环）**：`open_database` 在 `sqlite3.connect` 连接入参强制不低于 `1.0s` 锁等待下限（`SQLITE_BUSY_FLOOR_SECONDS`），并在执行表结构校验、初始化与 `WAL` 切换之前立即配置 `PRAGMA busy_timeout=5000`，消除初始化窗口的亚秒级锁超时隐患；`Admission.capture_authorization` 与 `resolve_capture_lease` 在打开数据库前建立单一单调时钟截止时间（`deadline`），并在建连后按剩余预算更新 `busy_timeout` 与进度中断器，不再在建连后重置完整超时窗口。
- **路由与实验账本文件状态诚实性（Python 3.14 兼容）**：`_existing_routing_document`、`read_bounded_jsonl` 与 `resolve_workspace_ledger` 改用显式 `os.lstat` + 常规文件校验替代 `Path.is_file()`，严格仅在 `FileNotFoundError` 时视为空缺；目录、非常规文件或权限/I/O 异常（含 Python 3.14 下 `is_file()` 静默吞除 `OSError` 的场景）均显式报错阻断，杜绝损坏或无权限历史被误判为“首次空历史”。
- **知识仓交付物契约测试结构化对齐**：`tests/test_corpus_deliverables.py` 将对知识单元正文的英文关键词匹配升级为结构化 JSON 消融回执字段校验（`candidate_id`、`leave_one_out_arms`、`fallback_boundary`），并支持 `<owner>/<repo>@<sha>:<path>` 规范代码仓证据定位符。
