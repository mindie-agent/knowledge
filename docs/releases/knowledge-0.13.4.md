# mindie-knowledge 0.13.4

**中文** · [English](knowledge-0.13.4.en.md)

共享组件自动更新的旁路 `uv` 构建现在沿用安装器的系统证书默认策略，支持使用企业系统信任库验证下载连接。用户显式设置的 `UV_SYSTEM_CERTS`、`UV_NATIVE_TLS`、CA 路径和代理继续优先。

构建失败仍向调用者报错，不发布候选运行时。版本选择、空闲切换、知识会话授权和贡献设置沿用现有机制。

验证记录：组件构建、正式 Release 选择、ETag 缓存与 Release 读取的 61 项定向测试通过；企业网络下真实 Release wheel 的 uv 下载探针确认默认信任库报 `UnknownIssuer`、使用系统证书成功。完整组件合同测试和本版本微基准由正式发布流水线执行，结果随 Release 资产交付。
