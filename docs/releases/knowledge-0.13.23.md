# mindie-knowledge 0.13.23：纯知识域职责收敛、`mindie-harness` RSI 兼容桥接、`< 1ms` 惰性导入与 v2 底座清单

[English](knowledge-0.13.23.en.md)

`mindie-knowledge v0.13.23` 完成知识引擎向纯知识职责边界的洁净架构收敛，并升级锁定 `mindie-diagnostics v0.5.16`：

- **纯知识域职责收敛与 `mindie-harness` RSI 平滑桥接**：将宿主无关的实验账本与单状态源优化循环下沉至共享底座 `mindie-harness`（`mindie_harness.rsi`），同时在 `mindie_knowledge` 中保留 100% 向后兼容的薄转发桥以保障存量调用方平滑升级；顶层 `import mindie_knowledge` 切换为 PEP 562 `< 1ms` 惰性按需加载。
- **`runtime_inventory` 接入 `mindie-harness` 底座与并发交接竞态修复**：组件运行时盘点与自动更新闭包正式纳入 `mindie-harness`（及 `opencode` 原生宿主插件）；修复 `loop/handoff.py` 中 `consumer.lock` 创建窗口期的二次探测重核对与 `cli.py` 陈旧本地套接字自关闭容错，收敛 Windows CI 并发组自取消问题。
- **底座升级与 `state-service` 门禁对齐**：升级 `mindie-diagnostics` 正式版引脚至 `v0.5.16`，全部核心模块保持 `< 800` 行并启用 `--profile state-service` PR 设计原则门禁。
