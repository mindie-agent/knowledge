# Windows 知识服务宿主归属

中文 | [English](windows-service-host.md) · [返回文档索引](README.md)

本文说明在原生 Windows 环境下，如何跨越短生命周期的宿主 Hook 启动并管理长期运行的后台知识服务。

## 1. 问题背景与 Task Scheduler 宿主

- 在原生 Windows 下，宿主 Hook 或短命令退出时可能回收同一控制台或作业关联的子进程。
- `mindie-knowledge` 通过 Windows Task Scheduler COM 接口注册当前用户 SID 私有的按需启动任务，使后台知识服务运行在独立于短 Hook 的宿主会话中。

## 2. DPAPI 单次加密上下文传输

- 启动参数与环境上下文通过当前用户 DPAPI 加密写入受 ACL 保护的传输文件，并绑定一次性 Nonce。
- 若任务管理器已接受 `Start` 请求但本地回执写入失败，状态报告为 `start_receipt_write_failed`（含不确定性标记），保留传输文件供已启动实例消费，绝不盲目重复触发 `Start`。

## 3. 无窗口标准流保护与可定位错误

- 针对 `pythonw` 无窗口进程可能将 `stdin`/`stdout`/`stderr` 置为 `None` 导致第三方日志初始化失败的问题，服务宿主在加载组件前自动为缺失流绑定 UTF-8 空设备句柄。
- 启动失败时，静态阶段码与异常类型跨越进程边界传回轮询调用方，确保调用方看到真实故障原因而非笼统的宿主超时。
