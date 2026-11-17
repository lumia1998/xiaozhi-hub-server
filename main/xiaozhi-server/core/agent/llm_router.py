"""多模型路由：按称呼把对话交给不同的大模型（LLM 配置里的条目，如 DeepSeekLLM）。

- 每个连接自己决定用哪个模型：切换时只换这个连接的 conn.llm，不影响其他设备；
- 各聊各的：每个模型在这个连接里有自己的一份对话记录（conn.dialogue 跟着换），切换时不带上下文过去；
  记录按设备和模型存进 data/chat.db，新连接、第一次切到这个模型时载入最近一段（chat_history）；
- 每台设备上次用的模型存到 data/agent_sessions.json，重连、服务重启后仍有效；
- 模型实例按条目名缓存、所有连接共用（和原来共享 ws_server._llm 一样）。后台默认模型直接用 ws_server._llm；
  其他模型第一次被叫到时才创建。条目配置变了（后台保存后热加载）就在下次叫到时重建，
  在线连接继续用旧实例，重连后用新的；
- 连接关闭时，这个连接用过的每个模型都会收到 on_conn_close。
"""

import asyncio
import copy
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from config.config_loader import get_project_dir
from config.logger import setup_logging
from core.agent import chat_history
from core.agent.session_store import SessionStore
from core.utils.llm import PI_TYPES

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

_pool = {}  # 条目名 -> (创建时的配置, 实例)
_pool_lock = threading.Lock()
_store = None


def store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore(Path(get_project_dir()) / "data" / "agent_sessions.json", "last_model")
    return _store


def _server_config(conn: "ConnectionHandler") -> dict:
    server = getattr(conn, "server", None)
    return getattr(server, "config", None) or getattr(conn, "common_config", None) or conn.config


def default_name(conn: "ConnectionHandler") -> Optional[str]:
    return (_server_config(conn).get("selected_module") or {}).get("LLM")


def _placeholder(value) -> bool:
    return value is None or str(value).strip() == "" or "你" in str(value)


def model_ready(config: dict, name: str) -> bool:
    """配置里有这个条目、类型是 Pi 认的，而且填好了：要有模型名和接口地址（模型服务的条目看服务里的），
    有 api_key 字段时不能是占位文字。"""
    entry = (config.get("LLM") or {}).get(name)
    if not isinstance(entry, dict):
        return False
    if entry.get('disabled') or entry.get("type", name) not in PI_TYPES:
        return False
    if entry.get('service_id'):
        service = (config.get('model_services') or {}).get(entry.get('service_id')) or {}
        return bool(service.get('base_url') and entry.get('model_name'))
    if _placeholder(entry.get("model_name")) or _placeholder(entry.get("base_url") or entry.get("url")):
        return False
    if "api_key" in entry and _placeholder(entry.get("api_key")) and name != "CustomOpenAILLM":
        return False
    return True


NOT_READY_REPLY = "还没配置大模型，请在电脑上打开网关后台填好 API Key。"


def not_ready_reply(conn: "ConnectionHandler") -> Optional[str]:
    """这个连接正在用的模型没填好（刚刷机、Key 还是占位文字）时，直接回给用户的话；填好了返回 None。
    不拦的话会拿空 Key 去请求，等出错才说"现在有点忙"。不知道在用哪个模型时不拦。"""
    st = _state(conn)
    name = st["current"] or default_name(conn)
    if not name:
        return None
    config = _server_config(conn)
    if not model_ready(config, name):
        default = default_name(conn)
        if default and default != name and model_ready(config, default):
            # 在后台另选了一个模型、配好后设成默认（没给原来那个填 Key）：在线的连接改用它
            try:
                if switch(conn, default, remember=False):
                    st.pop("not_ready", None)
                    return None
            except Exception as e:
                logger.bind(tag=TAG).error(f"切到默认模型 {default} 失败: {e}")
        st["not_ready"] = True
        return NOT_READY_REPLY
    if st.pop("not_ready", False):
        # 用户听到提示后在后台填好了，连接还没断：手上的实例是拿占位 Key 建的，换成按新配置建的
        try:
            instance = get_instance(conn, name)
            if instance is not conn.llm:
                _attach(conn, instance)
                conn.llm = instance
                logger.bind(tag=TAG).info(f"设备 {_device_key(conn)} 的模型 {name} 刚配置好，换用新实例")
        except Exception as e:
            st["not_ready"] = True  # 下一轮再试
            logger.bind(tag=TAG).error(f"模型 {name} 换用新实例失败，这一轮照旧请求: {e}")
    return None


def model_label(name: str) -> str:
    from core.admin import catalog

    vendor = catalog.VENDOR_BY_ENTRY.get(name)
    return vendor["label"] if vendor else name


def get_instance(conn: "ConnectionHandler", name: str):
    """取这个条目的模型实例：后台默认模型用 ws_server._llm，其他的从缓存取，配置变了就重建。"""
    config = _server_config(conn)
    server = getattr(conn, "server", None)
    if name == default_name(conn) and getattr(server, "_llm", None) is not None:
        return server._llm
    entry = copy.deepcopy((config.get("LLM") or {}).get(name))
    if entry and entry.get('type') in ('multi_api', 'pi') and entry.get('service_id'):
        service = (config.get('model_services') or {}).get(entry.get('service_id')) or {}
        # Service credentials and URL are part of the instance cache identity.
        entry.update(base_url=service.get('base_url'), api_key=service.get('api_key', ''))
    with _pool_lock:
        cached = _pool.get(name)
        if cached and cached[0] == entry:
            return cached[1]
    from core.utils import llm as llm_utils

    instance = llm_utils.create_instance(entry.get("type", name), entry)
    with _pool_lock:
        _pool[name] = (entry, instance)
    logger.bind(tag=TAG).info(f"已创建模型实例 {name}")
    return instance


def _state(conn: "ConnectionHandler") -> dict:
    st = getattr(conn, "hub_route", None)
    if st is None:
        st = {"current": None, "used": [], "dialogues": {}}
        conn.hub_route = st
    return st


def _attach(conn: "ConnectionHandler", instance) -> None:
    """记下这个连接用过的实例，连接关闭时逐个通知（on_conn_close）。"""
    st = _state(conn)
    if not any(x is instance for x in st["used"]):
        st["used"].append(instance)


def _device_key(conn: "ConnectionHandler") -> str:
    return getattr(conn, "device_id", None) or conn.session_id


def remembered(conn: "ConnectionHandler") -> Optional[str]:
    record = store().get(_device_key(conn)) or {}
    return record.get("model")


def _dialogue_for(conn: "ConnectionHandler", name: str):
    """这个模型在本连接里的对话记录。第一次切过去时新建，只带系统提示，不带聊天内容。"""
    from core.utils.dialogue import Dialogue

    st = _state(conn)
    dialogue = st["dialogues"].get(name)
    if dialogue is None:
        dialogue = Dialogue()
        dialogue.dialogue = [m for m in conn.dialogue.dialogue if m.role == "system"]
        chat_history.load(conn, dialogue, name)
        st["dialogues"][name] = dialogue
    prompt = getattr(conn, "prompt", None)
    if prompt:
        dialogue.update_system_message(prompt)
    return dialogue


def on_conn_open(conn: "ConnectionHandler") -> None:
    """连接建立时：记下默认实例，再切到这台设备上次用的模型（还可用的话）。"""
    st = _state(conn)
    st["current"] = default_name(conn)
    chat_history.load(conn, conn.dialogue, st["current"])
    if conn.llm is not None:
        _attach(conn, conn.llm)
    name = remembered(conn)
    if name and name != st["current"] and model_ready(_server_config(conn), name):
        try:
            switch(conn, name, remember=False)
            logger.bind(tag=TAG).info(f"设备 {_device_key(conn)} 沿用上次的模型 {name}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"切换到上次的模型 {name} 失败，使用默认模型: {e}")
    from core.agent import call_names

    call_names.schedule_warm_up(conn)  # 提前准备好只喊称呼时回的"我在"


def switch(conn: "ConnectionHandler", name: str, remember: bool = True) -> bool:
    """把这个连接切到指定模型。模型没配置好时返回 False，不切换。"""
    if not model_ready(_server_config(conn), name):
        return False
    st = _state(conn)
    if st["current"] != name or conn.llm is None:
        instance = get_instance(conn, name)
        _attach(conn, instance)
        if st["current"]:
            st["dialogues"][st["current"]] = conn.dialogue
        conn.dialogue = _dialogue_for(conn, name)
        conn.llm = instance
        logger.bind(tag=TAG).info(f"设备 {_device_key(conn)} 切换模型：{st['current']} → {name}")
        st["current"] = name
    elif name != default_name(conn):
        # 已经在用这个模型，但后台改过它的设置（实例要重建）：换成新实例，对话照旧。
        # 默认模型不在这里换：它由后台热加载重建，已连接的设备照旧用旧实例到重连（同 _apply 的说明）
        instance = get_instance(conn, name)
        if instance is not conn.llm:
            _attach(conn, instance)
            conn.llm = instance
            logger.bind(tag=TAG).info(f"设备 {_device_key(conn)} 的模型 {name} 配置有变化，换用新实例")
    if remember:
        store().put(_device_key(conn), {"model": name})
    return True


async def on_conn_open_async(conn: "ConnectionHandler") -> None:
    """在线程池里执行 on_conn_open：第一次创建模型实例可能要几百毫秒（如启动 Pi 的 Node 进程）。"""
    await asyncio.get_running_loop().run_in_executor(None, on_conn_open, conn)


async def switch_async(conn: "ConnectionHandler", name: str) -> bool:
    return await asyncio.get_running_loop().run_in_executor(None, switch, conn, name)


def on_conn_close(conn: "ConnectionHandler") -> None:
    """连接关闭时，这个连接用过的每个模型都要通知到（Pi 取消这个连接还没结束的请求）。"""
    st = getattr(conn, "hub_route", None)
    instances = list(st["used"]) if st else []
    if conn.llm is not None and not any(x is conn.llm for x in instances):
        instances.append(conn.llm)
    for instance in instances:
        if callable(getattr(instance, "on_conn_close", None)):
            try:
                instance.on_conn_close(conn)
            except Exception as e:
                logger.bind(tag=TAG).error(f"LLM 连接关闭钩子出错: {e}")
