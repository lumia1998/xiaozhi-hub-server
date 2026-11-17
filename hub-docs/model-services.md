# 模型服务与人设

后台「大模型」页面现在按接口服务管理模型，不再逐个填写厂商卡片。

1. 填写服务名称、接口地址、API Key，点击「获取全部模型并保存」。
2. 后台并行读取 OpenAI `/v1/models`、Gemini `/v1beta/models` 和 Claude `/v1/models` 列表，合并去重并处理分页。
3. 搜索并选中模型，点击「检测协议」或「自动检测并设为默认」。
4. 同时测试 OpenAI Chat Completions、OpenAI Responses、Gemini generateContent、Claude Messages。
   只有符合协议结构的成功响应才算通过。检测使用简短测试提示，最多四次生成请求，可能消耗额度。
5. 在通过检测的协议中按模型类别择优，结果持久化；不能只根据模型名字推断协议。

「全部模型」指该地址和密钥可见、且服务实际通过列表接口公开的模型。
接口不开放模型列表时无法凭空枚举。分页超过保护上限会明确显示未取完，不伪称完整。
刷新列表不自动把默认模型换成其他模型；协议检测失败也不会切换正在用的模型。
未检测的模型第一次被呼名或设备选择时会检测协议，后续复用实例的结果。

协议适配支持系统人设、完整工具调用/结果历史、流式文字、工具参数增量和连接取消。
Gemini 的 functionCall thoughtSignature 保留在本次连接的工具历史中。
运行中不在失败后盲目换协议重发用户请求；自动检测只发送独立的短测试请求。

人设在页面顶部独立保存到 `data/.config.yaml` 的 `prompt`，下次设备对话连接生效。
客户端唤醒词和显示名字独立于人设，不会因编辑人设而自动改变。

新配置：`model_services.<id>` 保存一个服务的 URL、凭据、模型列表及检测结果；
`LLM.<entry>` 保存模型和协议，并通过 `service_id` 引用该服务。密钥不返回前端。
已有默认模型迁移时保留条目名，因此其聊天历史和设备记住的模型标识仍有效。

Grok Bot 已移除后台入口、HTTP 路由、提供商加载和独立实现文件。
Claude Code 本地模式仍可使用；过去的 Grok 聊天记录只作为历史数据保留，不会执行。

协议参考：

- [OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Gemini Function Calling](https://ai.google.dev/gemini-api/docs/function-calling)
- [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages)

本次验证：四种协议均在用户现有网关通过真实普通回复和工具调用往返测试。
不同网关或模型仍需各自检测，不能从一个模型的结果推断全部模型都可用。
