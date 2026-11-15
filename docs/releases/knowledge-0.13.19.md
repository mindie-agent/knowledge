# mindie-knowledge 0.13.19：CANNBot/ACLGraph 三栈蒸馏器、性能建模分级知识语料与并行 CI 加速

[English](knowledge-0.13.19.en.md)

- **零外部依赖三栈知识蒸馏器与 `TorchAir -> ACLGraph` 语义规范化（`cannbot_aclgraph_distiller.py`、`distiller_*`、`knowledge_unit.py`、`catalog_snapshot.json`）**：新增纯标准库确定性蒸馏管线，支持在线参考仓库扫描（`scan_live` / `distill_live`）与离线目录快照提取（`static_ingestion` / `distill_static`），覆盖 53 个 `CANNBot-DSL` 算子、33 条 `Triton-Ascend` 规则、5 项 MFU 门禁及 `ACLGraph` 运行时原语共 110 个 `mindie-graded-unit/1` 知识单元，内置 7 条 `TorchAir -> ACLGraph` 规范化规则与极性/遗产边界保护。
- **四领域 `L1–L4` 性能建模与消融归因分级语料（`corpus/`）**：新增并校验 9 个 `mindie-graded-unit/1` 分级知识单元、路由清单（`routing-manifest.json`）与分片拓扑（`shard-topology.json`），覆盖 `vllm-ascend`（HBM 显存隐形开销公理、启动参数到 SOL 反事实 SOP、Day-0 MLAPO 预取多流变换及 `mindie-bench-ablation/2` 消融协议）、`operators`（ACLGraph 分桶 Padding/Varlen 公理与单算子 ABI 重放 SOP）、`infer-distributed`（EP 拐点、EPLB 与 xPyD 分离工作流）及 `infer-observability`（CANN 9.x 稳态采集门禁与 `mindie-diff-trace/2` 四路守恒残差归因）。
- **出站脱敏/YAML 解析吞吐优化、`xdist` 并行隔离与跨平台基线保留**：为蒸馏子句解析引入调用域隔离缓存（`ContextVar`），为任务物料头与流式校验引入 `CSafeLoader` / ASCII `CSafeDumper` 及已验证根目录相对路径快速通道，并为出站隐私扫描引入零分配字面量预筛与 ASCII 数字窗口收窄；测试隔离夹具（`tests/conftest.py`、`tests/package_fixture.py`、`tests/test_recovery_git.py`）支持 `pytest-xdist`（`-n auto --dist worksteal --durations=20`）按 Worker 目录隔离、本地裸仓回退与 Linux `uid 0` 能力集降权，并完整保留 `v0.13.18` Windows `CP936` / 用户态 Git、WSL2 非 `systemd` 守护及 `TimeoutError` 显式透传契约。
