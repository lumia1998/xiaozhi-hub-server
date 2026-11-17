import asyncio
import json
from urllib.parse import urlsplit


TOOL_NAME = "show_media"
KINDS = ("image", "video", "audio")
DEFINITION = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "向用户屏幕发送图片、视频或音频。使用用户提供或工具查到的真实 HTTP(S) 媒体直链，"
                       "不要编造地址，也不要把视频网站页面当视频直链。音视频由用户点按播放。发送后用简短文字说明，勿朗读链接。",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array", "minItems": 1, "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": list(KINDS)},
                            "url": {"type": "string", "description": "设备可访问的媒体直链"},
                            "title": {"type": "string", "description": "简短标题或图片替代文本"},
                        },
                        "required": ["kind", "url", "title"], "additionalProperties": False,
                    },
                },
            },
            "required": ["items"], "additionalProperties": False,
        },
    },
}


def supported(conn):
    capability = (getattr(conn, "features", None) or {}).get("media")
    return isinstance(capability, dict) and capability.get("version") == 1


def validate_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise ValueError("每次可发送 1 至 8 个媒体资源")
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            raise ValueError("媒体类型必须为 image、video 或 audio")
        url = item.get("url")
        title = item.get("title", "")
        if not isinstance(url, str) or len(url) > 4096 or any(ord(char) <= 32 for char in url) or "\\" in url:
            raise ValueError("媒体地址格式不正确")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("媒体地址必须是无账号密码的 HTTP(S) 直链")
        if not isinstance(title, str) or len(title) > 200:
            raise ValueError("媒体标题不能超过 200 字")
        result.append({"kind": item["kind"], "url": url, "title": title.strip()})
    return result


async def _send(conn, payload, stale):
    if stale():
        raise ValueError("本轮已取消")
    await conn.websocket.send(json.dumps(payload, ensure_ascii=False))


def execute(conn, arguments, call_id, sentence_id, stale):
    future = None
    try:
        if not supported(conn):
            raise ValueError("当前设备尚未支持媒体展示")
        if not isinstance(arguments, dict):
            raise ValueError("媒体参数格式不正确")
        items = validate_items(arguments.get("items"))
        allowed = conn.features["media"].get("kinds", [])
        if any(item["kind"] not in allowed for item in items):
            raise ValueError("当前设备不支持其中的媒体类型")
        payload = {"type": "media", "version": 1, "session_id": conn.session_id,
                   "sentence_id": sentence_id, "id": call_id, "items": items}
        future = asyncio.run_coroutine_threadsafe(_send(conn, payload, stale), conn.loop)
        future.result(timeout=5)
        return {"text": json.dumps({"status": "sent", "media": items}, ensure_ascii=False),
                "error": False, "terminate": False}
    except (ValueError, TypeError) as error:
        return {"text": str(error), "error": True, "terminate": False}
    except Exception:
        if future is not None:
            future.cancel()
        return {"text": "媒体发送结果未确认，请稍后重试", "error": True, "terminate": False}


def from_tool_result(content):
    try:
        result = json.loads(content)
        if isinstance(result, dict) and result.get("status") == "sent":
            return validate_items(result.get("media"))
    except (ValueError, TypeError):
        pass
    return []
