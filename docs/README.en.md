# MindIE Knowledge Documentation Index

[中文](README.md) | English · [Back to Repository Home](../README.en.md)

This directory contains the runtime contracts, public-transcript capture rules, community sharing protocol, repository review bot specification, and cross-platform process lifecycle documentation for `mindie-knowledge` (`mindie-agent/knowledge`). All documents are available in both English and Chinese.

## Core Contracts & Data Pipeline

| English Document | 中文 | Description |
|---|---|---|
| [MindIE Domain Loop](mindie-loop.md) | [MindIE 领域闭环契约](mindie-loop.zh.md) | Complete specification of two-layer storage, public sync, local BM25 retrieval, capture, outbound sharing, and independent feedback |
| [Public Transcript Capture](public-transcript-capture.md) | [公开对话材料与增量索引](public-transcript-capture.zh.md) | Deterministic public message selection, redaction, immutable block persistence, and incremental small-model navigation indexing |
| [Explicit Historical Transcript Import](history-import.md) | [显式历史会话导入](history-import.zh.md) | Contract for importing explicitly selected historical transcripts through the standard projection, redaction, and indexing pipeline |
| [Community Sharing](community-sharing.md) | [社区贡献与外发规范](community-sharing.zh.md) | Contributor-side staging, fail-closed redaction gate, `mindie-contribution/1` batch packaging, and read-only remote reconciliation |
| [Repository Bot Contract](repository-bot-contract.md) | [内容仓库审核机器人契约](repository-bot-contract.zh.md) | `mindie-content-review/2` automated review, merge, correction, and withdrawal contract |

## Compatibility & Cross-Platform Process Lifecycles

| English Document | 中文 | Description |
|---|---|---|
| [Release Compatibility](release-compatibility.md) | [版本准入与已安装操作锁](release-compatibility.zh.md) | Runtime compatibility verification, shared/exclusive operation locks, and idle cutover boundaries |
| [Service & Attempt Lifetime](service-process-lifetime.md) | [服务与单次操作生命周期](service-process-lifetime.zh.md) | Independent lifetimes for the background knowledge service, individual indexing attempts, and caller wait windows |
| [Summary Process Ownership](summary-process-ownership.md) | [摘要进程与临时目录归属](summary-process-ownership.zh.md) | Cross-platform summary subprocess trees, temporary profile directories, and crash cleanup guarantees |
| [Windows Service Host](windows-service-host.md) | [Windows 知识服务宿主归属](windows-service-host.zh.md) | Windows Task Scheduler launch contract, DPAPI context transfer, windowless stream protection, and exact retirement |
| [Windows Process Ownership](windows-process-ownership.md) | [Windows 进程树归属](windows-process-ownership.zh.md) | Windows named Job Objects and `KILL_ON_JOB_CLOSE` descendant reclamation |
| [Framework Stability](framework-stability.md) | [框架解析与稳定性边界](framework-stability.zh.md) | Pinned production parser fixtures, rebuildable ReMe derived indexes, and LangMem invocation stability |
