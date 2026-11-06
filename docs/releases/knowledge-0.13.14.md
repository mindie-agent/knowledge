# mindie-knowledge 0.13.14：Windows 后台服务真实 GUI 入口校验

[English](knowledge-0.13.14.en.md)

修复 [#115](https://github.com/mindie-agent/knowledge/issues/115)：后台知识服务原先只检查 `pythonw.exe` 文件存在，控制台型启动器也会被计划任务执行并弹窗。现在新任务注册前，以及恢复已有注册任务的实际启动前，都会读取 PE32/PE32+ 头并要求 GUI 子系统（`Subsystem == 2`）。异常入口报告 `interpreter:windowless_interpreter_invalid`，缺失入口报告 `interpreter:windowless_interpreter_missing`，拒绝路径不会注册或启动新任务。

- 校验只读且不缓存，覆盖异常旧环境和原地更新；恢复正确运行环境后可正常重试。保留虚拟环境身份、启动参数、正常停止及不重放未知启动的协议。
- 实验账本 Git 子进程补上 Windows `CREATE_NO_WINDOW`，覆盖无控制台服务父进程中的指纹计算。
- 增加真实 CUI `python.exe` 改名为 `pythonw.exe` 的负例、新任务拒绝、旧注册恢复与修复后重试测试；记录入口和子进程两个边界的防复发约束。
- 包含 main 已合入的 #117 测试修复：取消后的僵尸进程组不再被误判为存活进程树。

验证：Windows 相关启动、退役、进程所有权、Git、网络与 RSI 范围 204 passed、3 skipped。交互式桌面的真实计划任务验收 1 passed：服务就绪、原虚拟环境与参数保持、没有可见窗口或后代 `conhost.exe`，正常停止并删除临时任务。

实测环境为 Windows x86_64、uv 0.13.3、CPython 3.13.16；未重装 issue 原始 uv 0.11.11 / CPython 3.13.13 组合，不推断异常二进制的生成来源。Hosted CI 无交互登录令牌，真实计划任务验收按 `docs/windows-service-host.zh.md` 在交互式桌面显式运行。完整组件测试与精确标签源码 benchmark 由正式发布流水线执行并随包交付。
