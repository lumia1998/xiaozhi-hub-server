"""[xiaozhi-hub] 短句的合成结果缓存（解码后的 PCM）。

"我在"、确认语这类短句每次都一样，却每次都现合成：Edge TTS 约 1 秒，
解码再约 1 秒，而且合成线程只有一条，短句正在合成时，模型的正式回复只能排在后面等。
缓存 PCM 而不是 Opus 包：Opus 编码器每个连接一份、带状态，用连接自己的编码器现编才能和前后接得上。
"""

import threading
from collections import OrderedDict
from typing import Optional

MAX_CHARS = 16      # 只缓存这么短的句子（提示语、确认语），长回复每次都不一样
MAX_ENTRIES = 64

_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_lock = threading.Lock()


def key(tts, text: str, sample_rate: int) -> Optional[tuple]:
    """同一种合成、同一个音色和语速音量音调、同一个采样率下的同一句话。太长的句子不缓存，返回 None。"""
    if not text or len(text) > MAX_CHARS:
        return None
    return (
        type(tts).__module__,
        getattr(tts, "voice", None),
        getattr(tts, "speech_rate", None),
        getattr(tts, "volume", None),
        getattr(tts, "pitch_rate", None),
        getattr(tts, "cache_identity", None),
        sample_rate,
        text,
    )


def get(k) -> Optional[bytes]:
    if k is None:
        return None
    with _lock:
        pcm = _cache.get(k)
        if pcm is not None:
            _cache.move_to_end(k)
        return pcm


def put(k, pcm: bytes) -> None:
    if k is None or not pcm:
        return
    with _lock:
        _cache[k] = pcm
        _cache.move_to_end(k)
        while len(_cache) > MAX_ENTRIES:
            _cache.popitem(last=False)
