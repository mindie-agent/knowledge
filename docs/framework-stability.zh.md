# 框架稳定性与验证边界（Framework Stability & Verification Boundaries）

**中文** | [English](framework-stability.md) · [返回文档中心](README.md)

本文汇总 `mindie-knowledge` 在日常高频使用中的稳定性保障机制：包括生产解析器确定性隔离、ReMe 派生索引的自愈重建、Windows 下无阻塞的原子配置文件读写，以及组件级测试与实机宿主验收之间的清晰边界。

---

## 1. 日常使用的“零打扰”稳定性原则

- **一次配置，长期无感运行**：安装后首次进入原生入口（Claude Code / Kimi 的 `/mindie-agent` 或 Codex 的 `$mindie-agent`）记录一次授权选择，此后跨任务、跨分支、重启与常规升级均自动沿用，无需反复激活。社区贡献授权与故障诊断自动上报拥有各自独立的开关。
- **损坏配置绝不假装成首次安装**：当共享配置文件或 `mindie-consent/1` 被外部程序损坏时，系统如实报告配置故障并保留现场，绝不会清空已有数据或重新弹出首次安装引导。

---

## 2. 内部依赖框架的隔离策略

`mindie-knowledge` 将第三方或外部框架严格限制在纯函数或可随时重建的派生层内，防止外部框架状态污染核心数据：

| 依赖组件 | 在 `mindie-knowledge` 中的角色 | 稳定性与故障隔离设计 |
| --- | --- | --- |
| **宿主 Transcript 解析器** | 从 Claude / Codex / Kimi 原生会话中提取公开可见消息 | 使用确定性解析逻辑与版本锁定的生产解析器夹具（`tests/fixtures/production-parsers`）；遇到损坏或未知格式立即显式报错，绝不把原始 JSON 当作正文落盘。 |
| **ReMe 检索组件** | 提供本地 Markdown 分块、引用图构建与 BM25 词频检索 | 仅作为进程内纯算法库调用，**不启动任何后台守护进程、文件监控器或外网服务**；派生检索缓存一旦损坏或升级，直接从权威 Markdown 文件与 SQLite 检查点无损重建。 |
| **LangMem (`0.0.30`)** | 辅助生成新增分块的标题、摘要与任务导航 | 仅作为无状态的单批次增量索引函数调用宿主原生小模型，不启用其自带的图存储或长连接服务；所有结果与 Token 账本均由 `SummaryLedger` 事务统一控制。 |

---

## 3. Windows 配置文件的并发安全读写（Delete-Sharing + Atomic Rename）

在 Windows 上，如果一个进程正打开某个配置文件进行读取，Python 标准库的 `os.replace` 往往会因为目标文件被占用而抛出权限拒绝异常，或者导致读者读到半截文件。

为了实现与 POSIX 一致的无锁原子更新，`mindie-knowledge` 核心与各插件引导层共享同一套 Windows 原子文件读写实现：
1. **允许删除共享的读取（Delete-Sharing Reads）**：所有内部读者在打开配置文件时均启用 `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`；
2. **原生原子重命名替换（Atomic Rename）**：写入方先将新内容完整写入同目录下的临时文件，再通过原生 Win32 重命名语义原子替换目标路径。
- **效果**：正在读取的旧读者继续读完完整的旧文件内容，新打开路径的读者直接看到完整的新文件内容，双方互不阻塞、绝不会读到半截损坏的 JSON（详见 [Windows `_FILE_RENAME_INFORMATION` 规范](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)）。

---

## 4. 服务锁释放检测与测试验证边界

- **可靠的 `consumer.lock` 存活观测**：后台知识服务在完全关闭监听器与 SQLite 存储之前一直持有操作系统级 `consumer.lock`。`lock_held` 直接探测操作系统文件锁状态（而不去改写脆弱的 PID 文本文件），即使在 Windows 上连接已关闭的本地回环端口可能出现短暂超时，启动器也能立刻准确识别旧服务是否已释放锁。
- **确定性测试套件与实机验收分离**：
  ```bash
  # 在仓库根目录运行完整单元与集成测试套件
  MINDIE_FRAMEWORK_SOURCE=tests/fixtures/production-parsers python3 -m pytest -q tests
  ```
  - 当未设置 `MINDIE_FRAMEWORK_SOURCE` 时，测试默认使用仓库内提交的 `tests/fixtures/production-parsers` 并校验证书与三个解析器文件齐全；若显式传入了无效路径，测试会直接作为环境配置错误退出，绝不静默回退到系统安装目录。
  - 仓库内的 pytest 测试验证协议边界、故障注入、并发锁与进程树回收（零大模型调用）；而在真实 Claude Code / Codex / Kimi 宿主及昇腾 NPU 环境下的端到端表现为独立的实机验收证据，两者在发布记录中始终清晰区分。
