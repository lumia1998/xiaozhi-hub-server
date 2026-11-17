import asyncio
import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.agent import chat_history, media_output


ITEMS = [{"kind": "image", "url": "https://example.com/image.png", "title": "图片"},
         {"kind": "video", "url": "https://example.com/video.mp4", "title": "视频"},
         {"kind": "audio", "url": "https://example.com/audio.mp3", "title": "音频"}]


@pytest.mark.asyncio
async def test_send_media_and_cancel():
    conn = SimpleNamespace(features={"media": {"version": 1, "kinds": list(media_output.KINDS)}},
                           session_id="session", loop=asyncio.get_running_loop(),
                           websocket=SimpleNamespace(send=AsyncMock()))
    result = await asyncio.to_thread(media_output.execute, conn, {"items": ITEMS}, "call", "turn", lambda: False)
    assert not result["error"] and not result["terminate"]
    payload = json.loads(conn.websocket.send.call_args.args[0])
    assert payload == {"type": "media", "version": 1, "id": "call", "sentence_id": "turn",
                       "session_id": "session", "items": ITEMS}
    assert media_output.from_tool_result(result["text"]) == ITEMS
    conn.websocket.send.reset_mock()
    result = await asyncio.to_thread(media_output.execute, conn, {"items": ITEMS}, "call", "turn", lambda: True)
    assert result["error"]
    conn.websocket.send.assert_not_called()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "data:image/png;base64,abc",
                                "https://user:password@example.com/a", "https://example.com/ a", "https://[", "//example.com/a"])
def test_reject_invalid_url(url):
    with pytest.raises(ValueError):
        media_output.validate_items([{"kind": "image", "url": url}])


def test_unsupported_device_and_kind():
    conn = SimpleNamespace(features={})
    assert not media_output.supported(conn)
    assert media_output.execute(conn, {"items": ITEMS}, "call", "turn", lambda: False)["error"]
    conn.features = {"media": {"version": 1, "kinds": ["image"]}}
    assert media_output.execute(conn, {"items": ITEMS}, "call", "turn", lambda: False)["error"]


def test_history_preserves_media_with_old_text_rows(tmp_path, monkeypatch):
    store = chat_history.ChatDB(tmp_path / "chat.db")
    monkeypatch.setattr(chat_history, "db", lambda: store)
    try:
        store.insert("device", "model", "user", "看看图片")
        store.insert("device", "model", "assistant", None, tool_calls=[{
            "id": "call", "type": "function", "function": {"name": "show_media", "arguments": "{}"}}])
        store.insert("device", "model", "tool", json.dumps({"status": "sent", "media": ITEMS}), tool_call_id="call")
        store.insert("device", "model", "assistant", "这些是图片和视频")
        history = chat_history.history("device", 50)["model"]
        assert len(history) == 3
        assert history[0]["text"] == "看看图片"
        assert history[1]["media"] == ITEMS
        assert history[2]["text"] == "这些是图片和视频"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_pi_model_can_call_media_tool(monkeypatch):
    monkeypatch.setattr('config.logger.setup_logging', lambda: MagicMock())
    from core.agent import pi_chat
    from core.utils.dialogue import Dialogue

    class Provider:
        model_name = 'test'

        def events(self, session, dialogue, functions, tool, stale):
            assert any(item['function']['name'] == 'show_media' for item in functions)
            result = tool({'name': 'show_media', 'arguments': {'items': ITEMS}, 'call_id': 'call'})
            assert not result['error'] and not result['terminate']
            yield {'type': 'message', 'message': {'role': 'toolResult', 'toolCallId': 'call',
                                                 'content': [{'type': 'text', 'text': result['text']}]}}

    conn = SimpleNamespace(
        features={'media': {'version': 1, 'kinds': list(media_output.KINDS)}},
        config={}, session_id='session', loop=asyncio.get_running_loop(), websocket=SimpleNamespace(send=AsyncMock()),
        llm=Provider(), dialogue=Dialogue(), logger=MagicMock(), stop_event=threading.Event(),
        intent_type='nointent', tts=SimpleNamespace(tts_text_queue=queue.Queue(), store_tts_text=MagicMock()),
        _superseded=lambda _: False, _put_reply=MagicMock(),
    )
    monkeypatch.setattr(pi_chat.chat_history, 'device_of', lambda _: '')
    monkeypatch.setattr(pi_chat.chat_history, 'memory_context_for', lambda _: {})
    monkeypatch.setattr(pi_chat.llm_router, 'not_ready_reply', lambda _: None)
    monkeypatch.setattr(pi_chat.extensions, 'add_skills', lambda dialogue, _: dialogue)
    await asyncio.to_thread(pi_chat.chat, conn, '展示这段视频', 'turn')
    conn.websocket.send.assert_awaited_once()
    spoken = [item.content_detail for item in conn.tts.tts_text_queue.queue if item.content_detail]
    assert spoken == ['内容已发送到屏幕。']
