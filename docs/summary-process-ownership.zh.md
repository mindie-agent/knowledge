# 摘要进程与临时目录归属

中文 | [English](summary-process-ownership.md) · [返回文档索引](README.md)

本文说明 `mindie-knowledge` 调用宿主原生小模型生成分块导航摘要时的子进程树管理与临时目录清理契约。

## 1. 跨平台进程树强归属

- 摘要操作可能启动宿主 CLI 及其辅助子进程。为确保核心服务意外终止（如收到 `SIGKILL`）时不留下孤儿进程，共享执行层按平台采用原生强归属原语：
  - **Windows**：使用挂起创建（Suspended Spawn）并绑定带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的命名 Job Object。
  - **Windows WSL**：使用 `systemd` 临时用户服务（`KillMode=control-group`）。
  - **macOS / Linux**：使用共享 `owner-pipe` 守护辅助进程，当核心持有的唯一写端关闭（EOF）时立即终止整个操作进程组。

## 2. 结果保存与临时目录清理次序

- 正常完成时，严格遵循**先持久化模型返回结果与用量 -> 再终止并回收操作进程树 -> 最后删除本次操作专用的临时 Scratch 目录**的顺序。
- 若清理阶段发生次级错误，已保存的业务结果与原始错误均保持完整可见，不因清理异常覆盖主结果或诱导重复计费调用。
