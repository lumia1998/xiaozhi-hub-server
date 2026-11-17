"""Connection-bound delivery of persisted task results without the old reports API."""
import asyncio
import re
import uuid

from core.agent import chat_history, task_service
from core.handle.intentHandler import speak_txt
from core.handle.sendAudioHandle import send_stt_message
from core.utils import turn_gen


MAX_SPOKEN_RESULT = 1500
_NOISE = re.compile(r"(?:^|\n)\s*(?:tool|工具|参数|日志|trace|调用)\s*[:：].*", re.I)


def _result_text(payload):
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if isinstance(result, dict):
        result = result.get("text", "")
    text = str(result or "").strip()
    text = _NOISE.sub("", text)
    return text[:MAX_SPOKEN_RESULT].strip()


async def deliver_pending(conn, wait_for_tts=0):
    """Claim one result for an idle connection and start a fresh speech turn."""
    device = chat_history.device_of(conn)
    if not device or conn.client_is_speaking or conn.client_abort:
        return False
    if wait_for_tts:
        deadline = asyncio.get_running_loop().time() + min(5.0, max(0.0, wait_for_tts))
        while not conn.tts and not conn.stop_event.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
    if conn.stop_event.is_set() or conn.client_is_speaking or conn.client_abort or not conn.tts:
        return False
    context = chat_history.memory_context_for(conn)
    owner = context.get("authenticated_owner_id") or context.get("owner_id") or chat_history.owner_for_device(device)
    delivery = task_service.store().claim_delivery(owner, device, conn.session_id, current_owner=owner)
    if not delivery:
        return False
    text = _result_text(delivery.get("result"))
    if not text:
        task_service.store().mark_delivery(owner, delivery["id"], task_service.DELIVERY_CANCELLED)
        return False
    sentence_id = uuid.uuid4().hex
    conn.sentence_id = sentence_id
    conn.client_abort = False
    generation = turn_gen.bump(conn)
    turn_gen.bind(conn, sentence_id, generation)
    conn._task_delivery = (owner, delivery["id"], generation, sentence_id)
    try:
        await send_stt_message(conn, "后台任务结果：" + text)
        if conn.client_abort or turn_gen.stale(conn, generation):
            defer_delivery(conn)
            return False
        speak_txt(conn, text)
        return True
    except Exception:
        defer_delivery(conn)
        raise


def defer_delivery(conn):
    active = getattr(conn, "_task_delivery", None)
    if not active:
        return
    owner, delivery_id, _, _ = active
    try:
        task_service.store().mark_delivery(owner, delivery_id, task_service.DELIVERY_DEFERRED)
    finally:
        conn._task_delivery = None


def complete_delivery(conn, played_ms=None):
    active = getattr(conn, "_task_delivery", None)
    if not active:
        return
    owner, delivery_id, _, sentence_id = active
    if getattr(conn, "sentence_id", None) != sentence_id:
        defer_delivery(conn)
        return
    try:
        task_service.store().mark_delivery(owner, delivery_id, task_service.DELIVERY_PLAYED,
                                           played_ms=played_ms or 0)
    finally:
        conn._task_delivery = None
