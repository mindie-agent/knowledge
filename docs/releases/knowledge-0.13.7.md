# mindie-knowledge 0.13.7：保留原生升级中的 Bootstrap 基线语义

**中文** · [English](https://github.com/mindie-agent/knowledge/blob/v0.13.7/docs/releases/knowledge-0.13.7.en.md)

0.13.6 将 Kimi 的 active 集成覆盖项默认并入了 `source_baseline`。旧 Kimi 更新器先按不可变的 bootstrap requirements 构建候选环境，再将所选组件的 revision 与该基线比较。把 active pins 当作 bootstrap pins 导致比较结果错误地跳过组件构建。真机自动升级验收发现 Kimi 0.1.8 升级到 0.1.9 时激活了更旧的 bootstrap 组件。

`source_baseline(source)` 恢复为只返回 bootstrap requirements。原生宿主可显式调用 `source_baseline(source, active_overrides=True)`，叠加 `runtime/active-overrides.txt` 中官方仓库的精确源码 pins。新路径将 active 集成下限与旧控制器理解的 bootstrap 输入分开。显式请求会直接读取该文件，缺失或 pin 无效时明确报错。返回结构和源码身份算法保持不变。

配套 Kimi 修复将 active overrides 移至新路径且不保留旧顶层文件，并比较候选解释器的实际 inventory 与所选组件后再决定是否构建。两项修复缺一不可：仅发布新共享包无法改变仍在旧父环境中执行的代码。已经受损、bootstrap 运行时缺少必要 API 的 Kimi 0.1.9 实例需使用已有产品恢复入口一次；本版不宣称该实例完成了无干预自动恢复。

定向测试覆盖 bootstrap 与 active 源码选择、两种源码布局下旧控制器的候选构建判断，以及现有正式 Release 选择和运行时构建合同。正式发布工作流会验证精确 Tag 的依赖、本地合同、graded Feed 工作负载与分词基准。本次不新增付费模型评测或用户配置迁移。
