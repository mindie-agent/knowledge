# mindie-knowledge 0.13.18：Windows CP936/用户态 Git 与 WSL2 非 systemd 服务守护加固

[English](knowledge-0.13.18.en.md)

- **WSL2 原生工具查找与 PE/DrvFS 遮蔽排除（`_host_exec.py`、`gitbin.py`、`release_http.py`、`component_runtime.py`、`loop/process.py`）**：新增严格符号链接解析（`os.path.realpath(..., strict=True)`，不预先折叠 `..`）、`/proc/self/mountinfo`（`drvfs`/`9p`）与 4 KiB PE 头校验，杜绝 WSL2 `$PATH` 追加 `/mnt/c/.../git.exe`、`gh.exe`、`uv.exe` 遮蔽 Linux 原生可执行文件；Windows 新增 `%LOCALAPPDATA%\Programs\Git\{cmd,bin}\git.exe` 用户态 Git for Windows 探测。
- **Linux/WSL2 非 `systemd` 用户总线自适应守护与既有凭据不绕过（`loop/service_host.py`、`loop/systemd_service.py`、`loop/cli.py`、`loop/lifecycle.py`）**：当 Linux/WSL2 环境不存在可用 `systemd` 用户总线且无既有 `.systemd-service/launch.json` 归属凭据时，自动走独立会话直连服务路径（`spawn_service` + `consumer.lock` + `posix_owner`）；当 `XDG_RUNTIME_DIR` 未导出但 `/run/user/<uid>/bus` 有效归属当前用户时自动向 `systemctl`/`busctl`/`systemd-run` 透传 `XDG_RUNTIME_DIR`；既有 `.systemd-service/launch.json` 始终保持严格和解契约，绝不静默绕过。
- **Windows `CP936` UTF-8 编码与 `TimeoutError` 穿透（`component_runtime.py`、`loop/*`、`graded/*` 等）**：隔离解释器调用统一追加 `-X utf8`，补齐全部文本读写 `encoding="utf-8"` 与 `except OSError:` 前置 `except TimeoutError: raise`，升级 `mindie-diagnostics` 至正式版 `v0.5.12`（`3d82313bb4b826bf52d5ec1dd64ef6476a070b4b`）。
