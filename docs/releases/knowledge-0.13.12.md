# mindie-knowledge 0.13.12：Windows 零控制台弹窗守护与后台子进程句柄加固

**中文** · [English](https://github.com/mindie-agent/knowledge/blob/v0.13.12/docs/releases/knowledge-0.13.12.en.md)

- **Windows 零控制台弹窗加固（`release_http.py` / `gitread.py` / `windows_process.py`）**：为 `gh auth token` 凭据查询、只读 `git` 仓库探针及 Windows 后台任务控制器 `schtasks` 子进程调用统一显式附加 `subprocess.CREATE_NO_WINDOW`，彻底消除从 GUI 宿主或脱离控制台父进程触发组件自动更新与知识流同步时的控制台窗口闪现。
- **Windows 私有目录权限与陈旧连接探测加固（`loop/paths.py` / `service/server.py`）**：增强 Windows 平台下私有状态目录校验与非阻塞连接存活探测的稳定性（`#111`）。
