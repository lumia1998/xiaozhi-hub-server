import time
import json
import uuid
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.dialogue import Message
from core.providers.tts.dto.dto import SentenceType
from core.agent import call_names
from core.utils import turn_gen
from core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
from core.utils.util import remove_punctuation_and_length
from core.providers.tools.device_mcp import MCPClient, send_mcp_initialize_message

TAG = __name__

WAKE_REPLY_TIMEOUT = 8


async def handleHelloMessage(conn: "ConnectionHandler", msg_json):
    """处理hello消息"""
    audio_params = msg_json.get("audio_params")
    if audio_params:
        format = audio_params.get("format")
        conn.logger.bind(tag=TAG).debug(f"客户端音频格式: {format}")
        conn.audio_format = format
        conn.welcome_msg["audio_params"] = audio_params
    features = msg_json.get("features")
    if features:
        conn.logger.bind(tag=TAG).debug(f"客户端特性: {features}")
        conn.features = features
        if features.get("mcp"):
            conn.logger.bind(tag=TAG).debug("客户端支持MCP")
            conn.mcp_client = MCPClient()
        if features.get("aec"):
            conn.logger.bind(tag=TAG).debug("客户端启用了服务端AEC")
            conn.client_aec = True

    welcome = conn.welcome_msg
    if features and features.get("vad_end"):
        # [xiaozhi-hub] 告诉客户端这个网关会发 VAD 事件（含"什么也没识别出来"），它不必干等兜底超时
        welcome = {**welcome, "features": {**(welcome.get("features") or {}), "vad_end": True}}
    await conn.websocket.send(json.dumps(welcome))

    # The device waits for the server hello before processing MCP messages.
    if features and features.get("mcp"):
        asyncio.create_task(send_mcp_initialize_message(conn))
    # Results are persisted independently of this connection and claimed only
    # after the device is online and idle. No legacy report polling is used.
    asyncio.create_task(_deliver_task_result(conn))


async def _deliver_task_result(conn):
    try:
        from core.agent.task_delivery import deliver_pending
        await deliver_pending(conn, wait_for_tts=5)
    except Exception as exc:
        conn.logger.bind(tag=TAG).warning(f"后台任务结果投递失败: {type(exc).__name__}")


async def checkWakeupWords(conn: "ConnectionHandler", text):
    _, filtered_text = remove_punctuation_and_length(text)
    if filtered_text not in (conn.config.get("wakeup_words") or []):
        return False
    if not conn.config.get("enable_wakeup_words_response_cache", True):
        return False

    # 命中的唤醒词始终在这里结束，不因合成失败转交模型或播放其他音色的录音。
    generation = turn_gen.current(conn)
    sentence_id = uuid.uuid4().hex
    conn.sentence_id = sentence_id
    conn.client_abort = False
    conn.just_woken_up = True
    conn.just_woken_up_at = time.monotonic()

    def stale():
        return (conn.client_abort or conn.sentence_id != sentence_id
                or turn_gen.stale(conn, generation)
                or (getattr(conn, "stop_event", None) is not None and conn.stop_event.is_set()))

    if not conn.config.get("enable_greeting", True):
        await send_tts_message(conn, "stop")
        return True

    deadline = time.monotonic() + 3
    while not conn.tts and time.monotonic() < deadline and not stale():
        await asyncio.sleep(0.1)
    if stale():
        return True
    if not conn.tts:
        conn.logger.bind(tag=TAG).warning("唤醒问候：TTS 未就绪，跳过语音回复")
        await send_tts_message(conn, "stop")
        return True

    tts = conn.tts
    reply = call_names.reply_text(conn.config.get("call_names") or {})
    await send_tts_message(conn, "start")
    conn.client_is_speaking = True
    try:
        packets = await call_names.cached_reply(conn, reply, timeout=WAKE_REPLY_TIMEOUT)
    except Exception as exc:
        conn.logger.bind(tag=TAG).warning(f"唤醒问候合成失败，跳过语音回复: {type(exc).__name__}")
        packets = None
    if stale():
        return True
    if conn.tts is not tts or not packets:
        await send_tts_message(conn, "stop")
        return True
    if not (getattr(conn, "features", None) or {}).get("ready_on_tts_start"):
        await asyncio.sleep(call_names.CACHED_REPLY_DELAY)
    if stale():
        return True
    conn.logger.bind(tag=TAG).info(
        f"唤醒问候使用当前 TTS: {type(tts).__module__}, {getattr(tts, 'voice', '')}, {reply}")
    await sendAudioMessage(conn, SentenceType.FIRST, packets, reply, sentence_id)
    if not stale():
        await sendAudioMessage(conn, SentenceType.LAST, [], None, sentence_id)
    if not stale():
        conn.dialogue.put(Message(role="assistant", content=reply))
    return True
