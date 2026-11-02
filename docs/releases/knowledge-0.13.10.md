# mindie-knowledge 0.13.10：新增 `mindie-debugger` 组件自动更新闭环与 `xLLM` / `MindIE-SD` 一等框架支持

**中文** · [English](https://github.com/mindie-agent/knowledge/blob/v0.13.10/docs/releases/knowledge-0.13.10.en.md)

- **注册 `mindie-debugger` 至组件清单与命令重绑定（`runtime_inventory.py` / `component_bindings.py`）**：在 `COMPONENTS` 中新增 `"mindie-debugger": "mindie-agent/debugger"`，在 `COMPONENT_MODULES` 中新增 `"debugger": ("mindie-debugger", "mindie_debugger.cli")`，完成 `mindie-debugger` 的正式版发现、旁路运行时构建与模块入口重绑定闭环。
- **新增 `xLLM` 与 `MindIE-SD` 一等框架支持（`domain_catalog.py` / `conditions.py` / `retrieval.py`）**：扩展一等框架目录、条件匹配归一化别名（`xllm-service` -> `xllm`、`mindiesd` -> `mindie-sd`）与混合检索标识符分词及领域先验关键词。
