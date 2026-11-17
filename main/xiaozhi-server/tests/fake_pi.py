"""Pi 引擎（core/providers/llm/pi/pi.py 的 LLMProvider）的替身，给走 ConnectionHandler.chat() 的测试用。

events() 按 Pi 的顺序发事件：一段段文字（text），然后是整条 assistant 消息（message），最后 turn_end。
默认回答 reply；要按问题回答就重写 answer(dialogue)，一段段 yield 文字。asked 记下每轮收到的对话。
"""


class FakePi:
    model_name = "fake-model"

    def __init__(self, reply="好的"):
        self.reply = reply
        self.asked = []

    def answer(self, dialogue):
        yield self.reply

    def events(self, session_id, dialogue, tools=None, tool=None, stale=lambda: False):
        self.asked.append(dialogue)
        text = ""
        for part in self.answer(dialogue):
            text += part
            yield {"type": "text", "text": part}
        yield {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
        yield {"type": "turn_end"}
