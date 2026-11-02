# mindie-knowledge 0.13.9: Register Tencent WorkBuddy Repository & Compatibility Mapping

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.9/docs/releases/knowledge-0.13.9.md) · **English**

- **Register Tencent WorkBuddy plugin repository (`releases.py` / `releases_io.py`)**: Add `"workbuddy": "mindie-agent/mindie-agent-wb"` to `REPOSITORIES` and `"workbuddy": "harness-compatibility.json"` to `_COMPATIBILITY_PATHS`, enabling `validate_compatibility`, `validate_candidate_source`, and `select_release` for WorkBuddy.
