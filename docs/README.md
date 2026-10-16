# MindIE Knowledge 文档索引

中文 | [English](README.en.md) · [返回仓库首页](../README.md)

本目录汇总 `mindie-knowledge`（`mindie-agent/knowledge`）的核心运行时契约、公开对话采集、社区共享、审核机器人协议与跨平台服务生命周期文档。所有文档均提供中英双语版本。

## 核心契约与数据流

| 中文文档 | English | 说明 |
|---|---|---|
| [MindIE 领域闭环契约](mindie-loop.zh.md) | [MindIE Domain Loop](mindie-loop.md) | 双层存储、公开知识同步、本地检索、经验采集、外发与独立反馈的完整协议规范 |
| [公开对话材料与增量索引](public-transcript-capture.zh.md) | [Public Transcript Capture](public-transcript-capture.md) | 确定性公开消息筛选、隐私脱敏、不可变分块落盘与原生小模型增量索引 |
| [显式历史会话导入](history-import.zh.md) | [Explicit Historical Transcript Import](history-import.md) | 显式选择历史会话源并复用同一脱敏、分块与索引管线的契约 |
| [社区贡献与外发规范](community-sharing.zh.md) | [Community Sharing](community-sharing.md) | 贡献者端草稿暂存、脱敏门禁、`mindie-contribution/1` 批次构建与只读远程对账 |
| [内容仓库审核机器人契约](repository-bot-contract.zh.md) | [Repository Bot Contract](repository-bot-contract.md) | `mindie-content-review/2` 机器人校验、合入、修正与撤下协议 |

## 版本兼容与跨平台进程生命周期

| 中文文档 | English | 说明 |
|---|---|---|
| [版本准入与已安装操作锁](release-compatibility.zh.md) | [Release Compatibility](release-compatibility.md) | 运行时版本准入、操作共享/排他锁与空闲切换边界 |
| [服务与单次操作生命周期](service-process-lifetime.zh.md) | [Service & Attempt Lifetime](service-process-lifetime.md) | 后台知识服务、单次模型调用与调用方等待窗口的独立寿命语义 |
| [摘要进程与临时目录归属](summary-process-ownership.zh.md) | [Summary Process Ownership](summary-process-ownership.md) | 跨平台摘要子进程树、临时配置目录与异常退出回收机制 |
| [Windows 知识服务宿主归属](windows-service-host.zh.md) | [Windows Service Host](windows-service-host.md) | Windows Task Scheduler 宿主启动、DPAPI 传输、无窗口流保护与精确退役 |
| [Windows 进程树归属](windows-process-ownership.zh.md) | [Windows Process Ownership](windows-process-ownership.md) | Windows 命名 Job Object 与 `KILL_ON_JOB_CLOSE` 进程树回收边界 |
| [框架解析与稳定性边界](framework-stability.zh.md) | [Framework Stability](framework-stability.md) | 固定生产解析器夹具、ReMe 派生索引与 LangMem 调用稳定性保障 |
