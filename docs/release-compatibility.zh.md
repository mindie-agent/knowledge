# 版本兼容与跨平台文件锁（Release Compatibility & File Locks）

**中文** | [English](release-compatibility.md) · [返回文档中心](README.md)

本文说明 `mindie-knowledge` 提供的两项跨组件基础能力：
1. **`mindie_knowledge.releases`**：帮助宿主插件更新器根据当前客户端版本，确定性地挑选最高兼容的正式版插件 Release；
2. **`mindie_knowledge.loop.locks`**：在 POSIX（macOS / Linux）与 Windows 上提供统一的操作系统级共享/排他文件锁。

---

## 1. 插件版本兼容声明（`mindie-plugin-release/1`）

当用户升级 Claude Code 或 OpenAI Codex CLI 时，不同版本的客户端对 Hook 或插件接口的支持范围可能不同。为了防止插件盲目追新（`latest`）导致接口不兼容，每个官方正式版插件 Release 都会随附一份 `mindie-plugin-release.json` 资产：

```json
{
  "schema": "mindie-plugin-release/1",
  "version": "0.0.1",
  "source": {
    "repository": "mindie-agent/mindie-agent-codex",
    "sha": "0123456789abcdef0123456789abcdef01234567"
  },
  "compatibility": {
    "schema": "mindie-harness-compatibility/1",
    "harness": "codex",
    "supported": [{
      "minimum": "0.153.4",
      "maximum": "0.153.4",
      "maximum_inclusive": true,
      "include_prerelease": false
    }],
    "used_interfaces": ["hooks.stop"],
    "interface_registry": {
      "hooks.stop": {
        "supported": [{
          "minimum": "0.153.4",
          "maximum": "0.153.4",
          "maximum_inclusive": true,
          "include_prerelease": false
        }],
        "code_paths": ["scripts/entry.py:stop"],
        "test_ids": ["tests/test_stop.py::test_native_stop"],
        "evidence_refs": ["publisher-verified source or native evidence"]
      }
    }
  }
}
```

### 版本选择规则（`select_release`）

```python
from mindie_knowledge.releases import select_release, supports_harness
from mindie_knowledge.releases_io import fetch_releases, verify_release_source

records = fetch_releases("mindie-agent/mindie-agent-codex")
result = select_release(
    records,
    repository="mindie-agent/mindie-agent-codex",
    harness_name="codex",
    harness_version="0.153.4",
    installed_version="0.0.1",
)
if result["status"] == "candidate":
    metadata = verify_release_source(
        result["candidate"]["metadata"],
        repository="mindie-agent/mindie-agent-codex",
        harness_name="codex",
    )
```

- **只选正式版，绝不降级**：基于 `semantic-version==2.10.0` 解析 SemVer，自动排除 Draft 与 Prerelease 插件版本，挑选支持当前 `harness_version` 的最高稳定版本（返回 `candidate`、`up_to_date` 或 `no_compatible_release`），绝不会降级或重装相同版本。
- **接口区间强约束**：插件用到的每一个宿主接口（`used_interfaces`）都必须在 `interface_registry` 中登记，且插件声明的整体支持区间必须落在所有被使用接口的支持区间交集内。
- **源码双重核验（`verify_release_source`）**：选中候选版本后，进一步校验对应不可变 Git Commit 中的源码文件（Codex 为 `plugins/mindie-agent/harness-compatibility.json`，Claude Code 为 `harness-compatibility.json`）与 Release 资产完全一致。

---

## 2. 跨平台操作系统文件锁（`mindie_knowledge.loop.locks`）

为了在日常业务调用（共享锁）与插件静默更新/服务切换（排他锁）之间安全协调，且不引入脆弱的 PID 文本文件猜测，`mindie_knowledge.loop.locks` 导出了统一的跨平台文件锁原语：

```python
from mindie_knowledge.loop.locks import lock_file, try_lock_file, unlock_file

lock_file(fd, exclusive=True, blocking=False)  # 锁被占用时抛出 BlockingIOError
try_lock_file(fd, exclusive=False)             # 仅在发生 OS 锁竞争时返回 False
unlock_file(fd)
```

- **底层实现**：
  - **POSIX（macOS / Linux）**：使用 `fcntl.flock`；
  - **Windows**：使用 `pywin32` 的 `LockFileEx` / `UnlockFileEx` 锁定字节区间 `[0, 1)`，与插件引导层（Bootstrap）的字节锁完全兼容。
- **自动释放，无死锁残留**：多个共享读锁（`exclusive=False`）可并发重叠；只要有任何读锁未释放，排他更新锁（`exclusive=True`）就无法获取。当持有锁的进程退出或崩溃时，操作系统内核会自动释放文件锁，无需手动删除锁文件。
