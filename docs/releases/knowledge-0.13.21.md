# mindie-knowledge 0.13.21：组件附件缓存与 HTTP 帧校验

[English](knowledge-0.13.21.en.md)

- 组件 Catalog 在缓存 SHA256SUMS 和基准 JSON 附件之前验证大小、摘要与内容；清除旧版无效附件缓存，强制刷新也会更新附件。
- 非法 Content-Length 只在 HTTP 帧边界标记为传输失败，旧缓存可失效并重新读取。本地 JSON 解析故障仍保持内部故障分类。
- 覆盖真实 Catalog 附件链路的新响应、旧缓存、强制刷新，以及真实 HTTPResponse 的非法长度头。

这是 [mindie-agent#354](https://github.com/mindie-agent/mindie-agent/issues/354) 修复的独立审查补充。附件完整性失败继续可报告，历史脱敏事件的初始原因仍未确定。
