# mindie-knowledge 0.13.10: `mindie-debugger` Auto-Update Closure & First-Class `xLLM` / `MindIE-SD` Framework Support

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.10/docs/releases/knowledge-0.13.10.md) · **English**

- **Register `mindie-debugger` in runtime inventory and command bindings (`runtime_inventory.py` / `component_bindings.py`)**: Add `"mindie-debugger": "mindie-agent/debugger"` to `COMPONENTS` and `"debugger": ("mindie-debugger", "mindie_debugger.cli")` to `COMPONENT_MODULES`, completing formal release discovery, side-by-side runtime preparation, and standard module entrypoint rebinding for `mindie-debugger`.
- **Add first-class `xLLM` and `MindIE-SD` framework support (`domain_catalog.py` / `conditions.py` / `retrieval.py`)**: Extend the domain catalog, condition normalization aliases (`xllm-service` -> `xllm`, `mindiesd` -> `mindie-sd`), and compound identifier tokenization and domain prior keywords.
