# 模型调用前的插件钩子

语音识别后的入口 `core/handle/receiveAudioHandle.py` 在调用模型前运行
`core.agent.pre_llm.handle(conn, text, gen)`。命中后直接结束本轮模型分发，
回复交给当前 TTS，不调用 LLM 或记忆总结模型。

插件位于 `plugins_func/pre_llm/`。内置 `home_assistant.py` 使用现有
`plugins.home_assistant` 的设备、别名、地址和令牌。开关仍是
`plugins.home_assistant.direct_commands.enabled`。

```python
def match(conn, text):
    # 纯匹配：未命中返回 None，不能发送设备请求。
    ...

async def intercept(conn, text, stale=lambda: False):
    # 未命中
    return {"handled": False}
    # 命中并调用 HA REST 后
    return {"handled": True, "reply": "主卧主灯已打开。"}
```

新插件在 `core/agent/pre_llm.py` 的 `PLUGINS` 元组中注册，按顺序匹配。
第一个命中后独占本轮；发生异常或不确定结果时直接回复错误，不回退模型再次执行。
`stale()` 用于在用户打断、发起新一轮或关闭连接后停止后续动作。已发送的 HA 请求不重试。

当前例子：`主灯开`、`台灯关`、`智慧屏停`、`主灯开着吗`、`台灯亮度调到百分之五十`。
开/关对应实体域的 `turn_on`/`turn_off`，智慧屏的停对应 `media_player.media_stop`，
暂停仍对应 `media_pause`。设备必须出现在配置的列表中，操作需实体能力支持。
否定、条件、多设备、名称歧义或上下文表达未命中时，继续进入原来的模型流程。

日志 `pre_llm plugin=home_assistant handled=true ... elapsed_ms=... llm_calls=0`
记录的是插件匹配后的 HA 请求耗时，不包含前面的 ASR 和后面的 TTS。
网络失败/设备状态延迟可能超过一秒，不能保证所有情况下亚秒响应。
