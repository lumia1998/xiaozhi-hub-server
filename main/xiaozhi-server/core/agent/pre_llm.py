"""Pre-model hook runner. First handled response ends dispatch and goes directly to TTS.

Plugin interface:
  match(conn, text): cheap, side-effect-free route test
  async intercept(conn, text, stale): {'handled': False} or {'handled': True, 'reply': str}
An error after claiming a command is consumed, never retried through the model.
"""
import asyncio
import json
import time
import uuid

from core.utils import turn_gen
from plugins_func.pre_llm import home_assistant, weather

PLUGINS = (home_assistant, weather)
TAG = __name__


async def handle(conn, text, gen):
    heard = text
    if isinstance(text, str) and text.lstrip().startswith('{'):
        try: heard = json.loads(text).get('content', text)
        except (ValueError, AttributeError): return False
    plugin = next((plugin for plugin in PLUGINS if plugin.match(conn, heard) is not None), None)
    if plugin is None:
        return False
    from core.handle.sendAudioHandle import send_stt_message
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
    from core.utils.dialogue import Message
    from core.agent import chat_history

    stale = lambda: turn_gen.stale(conn, gen) or conn.stop_event.is_set()
    if not hasattr(conn, 'pre_llm_lock'): conn.pre_llm_lock = asyncio.Lock()
    async with conn.pre_llm_lock:
        if stale(): return True
        await send_stt_message(conn, heard)
        if stale(): return True
        conn.client_abort = False
        sentence_id = uuid.uuid4().hex
        conn.sentence_id = sentence_id
        turn_gen.bind(conn, sentence_id, gen)
        started = time.monotonic()
        try:
            result = await plugin.intercept(conn, heard, stale)
            if not isinstance(result, dict) or result.get('handled') is not True:
                raise ValueError('Claimed plugin returned an invalid result')
        except Exception as error:
            # Do not echo request tokens or upstream error bodies; never retry a possibly sent POST.
            conn.logger.bind(tag=TAG).warning(f'pre_llm plugin={plugin.NAME} error={type(error).__name__}; no LLM retry')
            result = {'handled': True, 'reply': getattr(plugin, 'ERROR_REPLY', '操作结果未确认，请稍后查询设备状态。')}
        if result.get('reply') is not None:
            try:
                if chat_history.device_of(conn):
                    await asyncio.to_thread(
                        chat_history.append_operation_event_for,
                        conn, result.get('action', plugin.NAME),
                        result.get('target', result.get('entity', '')),
                        result['reply'], 'user', bool(result.get('confirmed', False)),
                    )
            except Exception as error:
                conn.logger.bind(tag=TAG).warning(f'pre_llm event could not be saved: {type(error).__name__}')
        conn.logger.bind(tag=TAG).info(f'pre_llm plugin={plugin.NAME} handled=true '
            f'action={result.get("action", "none")} entity={result.get("entity", "none")} '
            f'elapsed_ms={round((time.monotonic()-started)*1000)} llm_calls=0')
        if stale() or result.get('cancelled') or result.get('reply') is None:
            return True
        reply = result['reply']
        if not isinstance(reply, str) or not reply:
            reply = '操作结果未确认，请稍后查询设备状态。'
        conn.dialogue.put(Message(role='user', content=heard))
        conn.dialogue.put(Message(role='assistant', content=reply))
        try: await asyncio.to_thread(chat_history.sync, conn)
        except Exception: conn.logger.bind(tag=TAG).warning('pre_llm history could not be saved')
        # HA is already executed; readiness/voice generation does not delay the actual action.
        deadline = time.monotonic() + 3
        while not conn.tts and time.monotonic() < deadline:
            if stale(): return True
            await asyncio.sleep(.05)
        if stale(): return True
        if not conn.tts:
            conn.logger.bind(tag=TAG).warning('pre_llm action handled but TTS unavailable')
            return True
        conn.tts.store_tts_text(sentence_id, reply)
        conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=sentence_id, sentence_type=SentenceType.FIRST, content_type=ContentType.ACTION))
        conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=reply)
        conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=sentence_id, sentence_type=SentenceType.LAST, content_type=ContentType.ACTION))
        return True
