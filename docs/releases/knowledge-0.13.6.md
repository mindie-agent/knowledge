# mindie-knowledge 0.13.6：统一精读预算与受管组件版本下限

**中文** · [English](https://github.com/mindie-agent/knowledge/blob/v0.13.6/docs/releases/knowledge-0.13.6.en.md)

单条 graded `ref` 不再绕过响应预算。单条和批量精读复用完整依赖闭包，兼容字段 `content`、`frontmatter` 在最终裁剪前计入成本；放不下的完整单元明确标为未加载，不截断可执行规则。当前版本已完整可见的目标仍保留其缺失的必要前提，无法装入的前提显式报告 `budget_excluded`。依赖使用真实版本引用，`known_refs` 只省略仍完整可见的同版本正文。

共享更新 API 支持宿主声明所需组件及外部 Python 环境的版本下限。标准 `python -m` 组件命令由共享 helper 识别，多个解释器的元数据读取共用 15 秒预算；版本元数据只用于防降级，不伪造源码 SHA。新受管组件仍必须来自通过现有身份、发布资产和 benchmark 校验的正式 Release。候选构建、空闲切换和配置比较交换继续由宿主现有更新器负责，自定义 wrapper 与无法证明归属的 console script 保持原样。

Kimi 的源码基线按 `runtime-active-overrides.txt` 覆盖不可变 bootstrap requirements，并保持官方仓库和完整 SHA 校验。版本 0.13.5 仅用于此前开发集成，未发布；0.13.6 为本轮源码与发布说明绑定新的正式发布身份。

已合入功能提交的本地定向检查为 120 passed、1 skipped；跳过项是外部知识内容 checkout 不在测试工作区。该提交的 Linux、macOS、Windows 合同检查及打包检查均通过。正式发布流水线仍会针对本 Tag 的精确源码执行依赖 Release 校验、完整本地合同测试、graded Feed 功能场景和分词微基准，并随 Release 交付结果。

本版本不宣称复杂任务整体降本或四端已安装环境已完成升级。已有小规模 CPU 配对中的复杂任务不退化门槛未通过，原始失败、预算耗尽与缺测结果保留。四端自动升级及安装后版本回读须使用配套插件正式版本独立验收。
