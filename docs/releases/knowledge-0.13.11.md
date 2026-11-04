# mindie-knowledge 0.13.11：统一单状态源 RSI 闭环引擎、分级检索默认预算提升与过期服务安全退役

**中文** · [English](https://github.com/mindie-agent/knowledge/blob/v0.13.11/docs/releases/knowledge-0.13.11.en.md)

- **统一单状态源 RSI 实验账本与闭环引擎（`graded/experiment_ledger.py` / `graded/rsi_loop.py` / `graded/explore.py`）**：将 `ExperimentLedger` 收敛为唯一权威状态存储，简化 `RSILoopEngine` 为 `orient` / `propose` / `verdict` 三原语闭环，统一处理被取代基线（`superseded`）、历史轮次重试保序与无瓶颈标签恢复，并在 `explore` 响应中直接返回 `active_rsi_session` 与下一轮引导建议。
- **提升分级检索默认 Token 预算与上限（`graded/context_manager.py` / `graded/explore.py` / `loop/cli.py` / `service/server.py`）**：将 `explore` 默认 `token_budget` 提升至 `24000`、`query` 提升至 `8000`、`explain` 提升至 `16000`，并将预算上限扩展至 `64000`，适配真实工程任务上下文容量。
- **修复被取代服务 Owner 残留导致 `service replace` 阻塞（`loop/cli.py` / `loop/systemd_service.py`）**：对 `state=replaced` 且已被新世代取代的旧服务及启动记录（`service-launch.json`）补齐身份校验与安全退役回收（`mindie-agent-codex#85`）。
