# Release Compatibility & Cross-Platform File Locks

[中文](release-compatibility.zh.md) | **English** · [Back to Documentation Center](README.en.md)

This guide covers two foundational utilities provided by `mindie-knowledge`:
1. **`mindie_knowledge.releases`**: Selects the highest compatible stable plugin Release for a given host client version;
2. **`mindie_knowledge.loop.locks`**: Provides a single cross-platform OS file-locking implementation across POSIX (macOS / Linux) and Windows.

---

## 1. Plugin Release Compatibility Contract (`mindie-plugin-release/1`)

Host clients such as Claude Code and OpenAI Codex CLI evolve rapidly. Rather than blindly tracking an unverified `latest` tag, every official stable plugin Release publishes a `mindie-plugin-release.json` asset declaring the exact host versions and interfaces it supports:

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

### Selecting & Verifying a Compatible Release

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

- **Highest Compatible Stable Release Only**: Using `semantic-version==2.10.0`, `select_release` excludes draft and prerelease plugin Releases, picks the highest stable version supporting `harness_version` (`candidate`, `up_to_date`, or `no_compatible_release`), and never downgrades or reinstalls the same version.
- **Interface Registry Enforcement**: Every interface listed in `used_interfaces` must be registered in `interface_registry`, and every declared plugin support interval must fit within the supported intervals of all used interfaces.
- **Source Integrity Verification (`verify_release_source`)**: Confirms that the immutable Git commit referenced by the Release asset contains an identical compatibility declaration (`plugins/mindie-agent/harness-compatibility.json` for Codex, `harness-compatibility.json` for Claude Code).

---

## 2. Cross-Platform OS File Locks (`mindie_knowledge.loop.locks`)

To coordinate everyday calls (shared reader locks) with idle service retirement and updates (exclusive writer locks) without brittle PID-file heuristics, `mindie_knowledge.loop.locks` exports a unified OS file-locking API:

```python
from mindie_knowledge.loop.locks import lock_file, try_lock_file, unlock_file

lock_file(fd, exclusive=True, blocking=False)  # Raises BlockingIOError when busy
try_lock_file(fd, exclusive=False)             # Returns False only on OS lock contention
unlock_file(fd)
```

- **Platform Primitives**:
  - **POSIX (macOS / Linux)**: Uses `fcntl.flock`;
  - **Windows**: Uses `pywin32` `LockFileEx` / `UnlockFileEx` on byte range `[0, 1)`, matching the bootstrap lock contract.
- **Kernel-Managed Release on Exit**: Multiple shared readers (`exclusive=False`) can overlap, while an exclusive updater (`exclusive=True`) blocks until all active readers finish. If a process exits or crashes, the OS kernel releases its file lock automatically without deleting the persistent lock file.
