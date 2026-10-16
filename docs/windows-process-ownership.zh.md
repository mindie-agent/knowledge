# Windows 进程树归属

中文 | [English](windows-process-ownership.md) · [返回文档索引](README.md)

本文说明原生 Windows 环境下摘要操作与子进程树的 Job Object 归属契约。

## 核心机制

- 共享运行时使用 `pywin32` 创建带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 标志的命名 Windows Job Object，并在子进程挂起创建阶段将其纳入 Job。
- 适配器不再通过脆弱的 PPID 轮询二次推断同一进程树归属。
- 当所属核心工作器退出、被取消或被强制终止时，操作系统自动关闭 Job 句柄并可靠回收整个后代进程树（包括已脱离原进程组的孙进程），同时保持独立的后台知识服务不受影响。
