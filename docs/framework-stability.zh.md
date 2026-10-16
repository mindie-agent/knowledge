# 框架解析与稳定性边界

中文 | [English](framework-stability.md) · [返回文档索引](README.md)

本文说明 `mindie-knowledge` 对内部依赖框架（生产对话解析器、ReMe 检索组件与 LangMem 索引函数）的稳定性隔离策略。

## 1. 生产解析器确定性

- 公开对话筛选使用经过版本固定的确定性解析逻辑与测试夹具（`tests/fixtures/production-parsers`），不依赖外部可变服务。
- 未知或损坏的 Transcript 格式显式报错，绝不静默跳过或把整段原始 JSON 当作公开正文落盘。

## 2. ReMe 派生索引隔离

- ReMe 仅作为进程内库用于 Markdown 分块、引用图构建与 BM25 检索，不启动任何后台守护进程、文件监控器或外网账号服务。
- 派生索引损坏或版本升级时，直接从权威 Markdown 正文与 SQLite 账本全量重建，不影响权威数据安全。

## 3. LangMem 纯函数化调用

- LangMem 仅作为增量导航生成函数调用宿主原生小模型，不启用其自带的图存储或后台长连接服务，所有状态提交均由 `mindie-knowledge` 的事务账本统一控制。
