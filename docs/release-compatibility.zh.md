# 版本兼容、组件运行时更新与跨平台文件锁（Release Compatibility, Component Runtime Updates & File Locks）

**中文** | [English](release-compatibility.md) · [返回文档中心](README.md)

本文说明 `mindie-knowledge` 提供的三项跨组件基础能力：
1. **`mindie_knowledge.releases` 与 `mindie_knowledge.release_http`**：结合有界 `ETag`/TTL 公开元数据缓存，帮助宿主插件更新器根据当前客户端版本确定性地挑选最高兼容的正式版插件 Release；
2. **`mindie_knowledge.component_releases`、`component_runtime` 与 `runtime_inventory`**：直接从 8 个官方组件仓库发现最新正式版 GitHub Release、核验不可变 Tag 与 `benchmark-results.json` 门禁，并在活动环境旁构建并排（Side-by-Side）隔离 `uv` 运行时；
3. **`mindie_knowledge.loop.locks`**：在 POSIX（macOS / Linux）与 Windows 上提供统一的操作系统级共享/排他文件锁。

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

- **只选正式版，绝不降级**：基于 `semantic-version==2.10.0` 解析 SemVer，自动排除 Draft 与 Prerelease 插件版本，挑选支持当前 `harness_version` 的最高稳定版本（返回 `candidate`、`up_to_date` 或 `no_compatible_release`），绝不会降级或重装相同版本。向 `fetch_releases` 传入 `minimum_version` 可跳过低于该版本的 `mindie-plugin-release.json` 资产下载。
- **接口区间强约束**：插件用到的每一个宿主接口（`used_interfaces`）都必须在 `interface_registry` 中登记，且插件声明的整体支持区间必须落在所有被使用接口的支持区间交集内。
- **源码双重核验（`verify_release_source`）**：选中候选版本后，进一步校验对应不可变 Git Commit 中的源码文件（Codex 为 `plugins/mindie-agent/harness-compatibility.json`，Claude Code 与 Kimi Code 为 `harness-compatibility.json`）与 Release 资产完全一致。
- **有界 `ETag`/TTL 公开元数据缓存（`release_http.py`）**：插件与组件更新器共享 `mindie_knowledge.release_http.read`（默认缓存目录 `~/.cache/mindie-agent/releases` 或 `MINDIE_RELEASE_CACHE`，上限 `128` 个条目、`64 MiB`）。不可变路径（`/git/`、`/contents/`、`/releases/download/`）采用 `86400s`（24 小时）TTL；可变 Release 接口在有 GitHub Token 时采用 `300s` TTL、无 Token 时采用 `900s` TTL，过期后携带 `If-None-Match`（`ETag`）发起条件重验（命中 `304 Not Modified` 直接复用本地字节）。过期的缓存绝不会掩盖网络读取失败，断网或超限响应（`ReleaseResponseTooLarge`）会显式报错。

---

## 2. 独立组件 Release 发现与并排（Side-by-Side）运行时构建

`runtime_inventory.COMPONENTS` 登记了 8 个官方第一方组件（`mindie-knowledge`、`remote-dev`、`mindie-diagnostics`、`mindie-profiler`、`mindie-sim`、`npu-top`、`mindie-coordinator` 与 `mindie-bench`）。宿主插件更新器通过 `mindie_knowledge.component_releases`、`mindie_knowledge.component_runtime` 与 `mindie_knowledge.runtime_inventory` 发现、核验并准备组件运行时：

### 2.1 组件正式版发现与基准门禁核验（`component_releases.py` & `component_runtime.discover`）

- **单调不降级版本选择（`component_releases.select`）**：
  - `component_runtime.discover(python, minimum, ...)` 先通过 `python -I runtime_inventory.py`（`read_inventory`）只读提取当前运行环境中已安装第一方组件的版本与 `direct_url.json` 中的 40 位 Commit SHA，并结合插件声明的基线版本（`minimum`，由 `source_baseline` 从 `runtime-requirements.txt` 或 `pyproject.toml` 的 `[tool.uv.sources]` 读取）。
  - 对每个已安装或基线声明的组件，`Catalog.latest(name)` 请求 `/repos/<repo>/releases/latest`，由 `select()` 取当前下界（`max(installed, baseline)`）与 `latest` 中的较高版本，绝不降级；若相同版本号对应的源码提交哈希发生漂移，立即抛出 `ValueError` 拒绝采用。
- **正式 Release 资产、不可变 Tag 与基准报告逐项核验（`Catalog.latest`）**：
  1. **正式版与必需资产完整性**：仅接受 `draft is False`、`prerelease is False` 且无预发布/构建后缀的稳定 SemVer 标签；强制要求 Release 资产同时包含对应版本的 `.whl`、`.tar.gz`（sdist）、`SHA256SUMS`、`benchmark-results.json`、`benchmark-results.md` 与 `benchmark-SHA256SUMS`（且文件大小均 `> 0`）。
  2. **不可变 Tag 解引用与 `pyproject.toml` 核对**：通过 `/git/ref/tags/<tag>`（注解标签继续通过 `/git/tags/<sha>` 解引用直至 `type == "commit"`）获取 40 位源码 Commit SHA（`revision`），并核对该提交下 `pyproject.toml` 的 `project.name` 与 `project.version` 与标签一致。
  3. **必需基准报告下载与 SHA-256 校验**：实际下载 `SHA256SUMS` 与 `benchmark-results.json`，校验 `SHA256SUMS` 覆盖 wheel、sdist 与基准文件且与 `benchmark-results.json` 的真实 SHA-256 一致，并确认报告内 `release_gate is True`、`version` 一致且测量前后工作区干净（`source` 与 `source_after` 的 `revision` 一致且 `dirty is False`）。

### 2.2 并排隔离虚拟环境构建与独立进程核验（`component_runtime.build` & `runtime_inventory.py`）

- **并排 Generation 目录构建**：`component_runtime.build(source, root, baseline, targets, ...)` 在插件源码树之外创建 `<root>/<generation_key>` 目录，将插件源码输入文件复制到 `generation/source`（上限 `16 MiB`），在并排的 `generation/runtime/.venv` 中以验证通过的精确 Git Commit SHA（`git+https://github.com/<repository>@<revision>`）执行 `uv sync --project <runtime> --no-editable --no-dev`。若同代候选环境的 `components.json` 已存在，则在核对保留的 `generation/source` 字节与当前源码一致后直接复用。
- **锁文件与隔离清单核验**：在候选 `.venv` 中运行 `python -I runtime_inventory.py <manifest>`，由 `verify_lock` 校验 `uv.lock` 的完整 SHA-256 摘要（`lock_sha256`），由 `verify` 核对每个目标组件的已安装版本与 `direct_url.json` 提交哈希（`vcs_info.commit_id`），通过后原子写出 `components.json`（`schema: "mindie-component-runtime/1"`）。
- **宿主切换归属与后台 Knowledge Worker 边界**：四个宿主插件适配器（`mindie-agent-codex`、`mindie-agent-cc`、`mindie-agent-kimi`、`mindie-agent-dsh`）各自负责候选环境的原生 API 探针、排他更新锁获取、空闲服务退役与绑定指针的 `os.replace` 原子切换。组件更新发现与并排环境构建**绝不主动启动后台知识服务（Knowledge Worker）**，也**绝不改变会话级手动 `/mindie-agent` 激活门禁**。

---

## 3. 跨平台操作系统文件锁（`mindie_knowledge.loop.locks`）

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
