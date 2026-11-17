# 语音服务配置

管理后台 → 语音合成：选择服务，填写参数，点击「试听」，确认后「保存并启用」。
试听不修改配置；保存后新设备连接使用所选服务，已经开始的对话使用原有实例。
音箱结束当前对话后再次唤醒即可采用新音色。

| 服务 | 凭据和常用参数 |
| --- | --- |
| Edge TTS | 无需密钥；音色、语速、音调 |
| 火山豆包 | App ID、Access Token、资源 ID、音色 ID、语速、音量、可选情感/语言 |
| 海螺 / MiniMax | API Key、可选 Group ID、模型、音色 ID、语速、音量、音调、可选情感/语言增强 |
| OpenAI 兼容 | 完整 `/audio/speech` 接口地址、可选 API Key、模型、音色、语速、可选 instructions |

火山的资源、模型代际和音色必须相匹配，并在对应账号下开通；复刻音色可直接填写 ID。
MiniMax 的模型候选仅供填写参考，具体可用性以账号权限为准。海螺网页会员和开放平台 API 凭据不能混用。
密钥不会返回给浏览器，留空保留旧密钥。修改接口地址时，需要重新填写已有密钥。

接口协议参考：

- [media-luna 火山 V3](https://github.com/ziyi233/media-luna/blob/main/src/plugins/connector-volcengine-tts/connector.ts)：`/api/v3/tts/unidirectional`，NDJSON、Base64 音频。
- [media-luna MiniMax](https://github.com/ziyi233/media-luna/blob/main/src/plugins/connector-minimax/connector.ts)：`/v1/t2a_v2`，非流式 JSON、Hex 音频。
- [media-luna OpenAI 兼容](https://github.com/ziyi233/media-luna/blob/main/src/plugins/connector-openai-tts/connector.ts)：`/audio/speech`，二进制音频。

新的 `cloud_http` 适配器按句获取 MP3，再使用原有 PCM/Opus 输出和打断流程；火山的 HTTP
响应虽分块接收，本实现收齐一句后才送往音箱，不等同于双向流式 TTS。
它也支持整句合成，可用于提醒铃声预合成。原有旧版火山/MiniMax 配置和模块保留，未自动迁移密钥。

测试：`test/test_tts_providers_admin.py` 使用本地模拟服务验证协议、错误处理、缓存、配置和鉴权；
付费提供商的真实合成需要用户自己的有效凭据。
