# mindie-knowledge 0.13.20：发布元数据缓存与解析来源修复

[English](knowledge-0.13.20.en.md)

- 在公共 HTTP 边界验证 JSON 后才写入缓存；旧版无效缓存被移除并无条件重新读取，避免坏响应被 ETag 或 TTL 持续复用。
- 检查 Content-Length，拒绝有界 HTTP 读取返回的短响应。有效缓存、304 校验、容量限制与失败可见性保持不变。
- 仅将 HTTP 边界确认的 JSON 拒绝标记为远端数据失败；本地 ValueError、RecursionError 和来源不明的 invalid_release_json 保留为内部故障。
- 插件发布与共享组件 Catalog 使用同一验证边界。覆盖坏响应恢复、旧缓存、真实 HTTPResponse 短读、ETag/304 和本地解析故障不重取行为。

关联诊断：[mindie-agent#354](https://github.com/mindie-agent/mindie-agent/issues/354)。脱敏事件无法证明历史响应正文或初始损坏原因；本版本修复已复现的缓存放大路径，不将历史事件一概归因网络。
