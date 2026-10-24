# Release Compatibility, Component Runtime Updates & Cross-Platform File Locks

[中文](release-compatibility.zh.md) | **English** · [Back to Documentation Center](README.en.md)

This guide covers three foundational mechanisms provided by `mindie-knowledge`:
1. **`mindie_knowledge.releases` & `mindie_knowledge.release_http`**: Selects the highest compatible stable plugin Release for a given host client version using a bounded `ETag`/TTL public metadata cache;
2. **`mindie_knowledge.component_releases`, `component_runtime` & `runtime_inventory`**: Discovers independent formal GitHub Releases across the 8 official components, verifies immutable tags and `benchmark-results.json` gates, and builds side-by-side isolated `uv` runtimes;
3. **`mindie_knowledge.loop.locks`**: Provides a single cross-platform OS file-locking implementation across POSIX (macOS / Linux) and Windows.

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

records = fetch_releases("mindie-agent/mindie-agent-codex", minimum_version="0.0.1")
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

- **Highest Compatible Stable Release Only**: Using `semantic-version==2.10.0`, `select_release` excludes draft and prerelease plugin Releases, picks the highest stable version supporting `harness_version` (`candidate`, `up_to_date`, or `no_compatible_release`), and never downgrades or reinstalls the same version. Passing `minimum_version` to `fetch_releases` skips downloading `mindie-plugin-release.json` assets for releases older than `minimum_version`.
- **Interface Registry Enforcement**: Every interface listed in `used_interfaces` must be registered in `interface_registry`, and every declared plugin support interval must fit within the supported intervals of all used interfaces.
- **Source Integrity Verification (`verify_release_source`)**: Confirms that the immutable Git commit referenced by the Release asset contains an identical compatibility declaration (`plugins/mindie-agent/harness-compatibility.json` for Codex, `harness-compatibility.json` for Claude Code and Kimi Code).
- **Bounded `ETag`/TTL Public Metadata Cache (`release_http.py`)**: Plugin and component updaters share `mindie_knowledge.release_http.read` (default cache directory `~/.cache/mindie-agent/releases` or `MINDIE_RELEASE_CACHE`, capped at `128` entries and `64 MiB`). Immutable paths (`/git/`, `/contents/`, `/releases/download/`) use an `86400s` (24h) TTL; mutable Release endpoints use a `300s` TTL when a GitHub token is available or `900s` unauthenticated, sending `If-None-Match` (`ETag`) conditional requests after expiry and reusing cached bytes on `304 Not Modified`. Expired cache entries never conceal network failures—offline or oversized responses (`ReleaseResponseTooLarge`) fail visibly.

---

## 2. Independent Component Release Discovery & Side-by-Side Runtime Preparation

`runtime_inventory.COMPONENTS` registers the 8 official first-party components (`mindie-knowledge`, `remote-dev`, `mindie-diagnostics`, `mindie-profiler`, `mindie-sim`, `npu-top`, `mindie-coordinator`, and `mindie-bench`). Host plugin updaters discover, verify, and prepare component runtime updates through `mindie_knowledge.component_releases`, `mindie_knowledge.component_runtime`, and `mindie_knowledge.runtime_inventory`:

### 2.1 Direct GitHub Release Discovery & Benchmark Verification (`component_releases.py` & `component_runtime.discover`)

- **Monotonic Non-Downgrading Selection (`component_releases.select`)**:
  - `component_runtime.discover(python, minimum, ...)` reads the currently installed first-party components and their 40-character `direct_url.json` commit SHAs via `python -I runtime_inventory.py` (`read_inventory`), and combines them with the adapter's declared baseline pins (`minimum`, read via `source_baseline` from `runtime-requirements.txt` or `pyproject.toml` `[tool.uv.sources]`).
  - For each component in the installed and baseline set, `Catalog.latest(name)` queries `/repos/<repo>/releases/latest` and `select()` keeps the higher version between the current floor (`max(installed, baseline)`) and `latest`. It never downgrades, and raises `ValueError` if a commit SHA changes without a version bump.
- **Formal Release Asset, Tag & Benchmark Gate Verification (`Catalog.latest`)**:
  1. **Formal Release & Required Assets**: Requires `draft is False`, `prerelease is False`, and a stable SemVer tag (no prerelease or build metadata), and verifies that the Release assets include a matching `.whl`, `.tar.gz` (sdist), `SHA256SUMS`, `benchmark-results.json`, `benchmark-results.md`, and `benchmark-SHA256SUMS` (all with `size > 0`).
  2. **Immutable Tag Dereferencing & `pyproject.toml` Check**: Resolves `/git/ref/tags/<tag>` (peeling annotated tag objects via `/git/tags/<sha>` until reaching `type == "commit"`) to obtain the 40-character commit `revision`, and verifies that `pyproject.toml` at that commit has matching `project.name` and `project.version`.
  3. **Required Benchmark Report & SHA-256 Verification**: Downloads `SHA256SUMS` and `benchmark-results.json`, verifies that `SHA256SUMS` covers the wheel, sdist, and benchmark files and matches the SHA-256 digest of `benchmark-results.json`, and confirms that `benchmark-results.json` has `release_gate is True`, matching `version`, and clean `source` / `source_after` records (`revision == revision` and `dirty is False`).

### 2.2 Side-by-Side Runtime Build & Isolated Verification (`component_runtime.build` & `runtime_inventory.py`)

- **Side-by-Side Generation Directory**: `component_runtime.build(source, root, baseline, targets, ...)` creates `<root>/<generation_key>` outside the adapter source, storing a verified copy of the adapter source inputs in `generation/source` (`<= 16 MiB`) beside `generation/runtime/.venv`, and runs `uv sync --project <runtime> --no-editable --no-dev` pinning the verified exact Git commit SHAs (`git+https://github.com/<repository>@<revision>`). If a completed `components.json` already exists for that generation, `build()` reuses it after verifying that the retained `generation/source` bytes match `source`.
- **Lock & Isolated Inventory Verification**: Runs `python -I runtime_inventory.py <manifest>` inside the candidate `.venv`, where `verify_lock` checks the full `uv.lock` SHA-256 against `lock_sha256` and `verify` confirms that every selected component's installed version and `direct_url.json` `vcs_info.commit_id` match `targets`, then atomically writes `components.json` (`schema: "mindie-component-runtime/1"`).
- **Host Cutover Ownership & Knowledge Worker Boundary**: Each host plugin adapter (`mindie-agent-codex`, `mindie-agent-cc`, `mindie-agent-kimi`, and `mindie-agent-dsh`) retains ownership of its native API probe, update lock / idle service retirement, and atomic binding pointer swap (`os.replace`). Component update discovery and side-by-side runtime builds **never proactively start the background knowledge worker** and **never alter the per-session manual `/mindie-agent` activation gate**.

---

## 3. Cross-Platform OS File Locks (`mindie_knowledge.loop.locks`)

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
