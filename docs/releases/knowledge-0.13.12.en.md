# mindie-knowledge 0.13.12: Windows Zero Console Popup Hardening & Background Subprocess Guards

[中文](https://github.com/mindie-agent/knowledge/blob/v0.13.12/docs/releases/knowledge-0.13.12.md) · **English**

- **Windows Zero Console Popup Hardening (`release_http.py` / `gitread.py` / `windows_process.py`)**: Explicitly passes `subprocess.CREATE_NO_WINDOW` to `gh auth token` credential probes, read-only `git` repository calls, and Windows Task Scheduler `schtasks` commands so automatic component update checks and knowledge feed synchronization never flash a transient console window from GUI hosts.
- **Windows Private Directory & Stale-Connection Probe Hardening (`loop/paths.py` / `service/server.py`)**: Hardens Windows private state-directory checks and non-blocking socket liveness probes (`#111`).
