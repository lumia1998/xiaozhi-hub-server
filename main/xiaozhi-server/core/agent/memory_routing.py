"""Local topic index for shared memory; deciding a route never performs I/O.

The latest user message owns the decision. Only a short, referential follow-up
may use the immediately previous user message, and that previous message must
qualify on its own. Assistant/tool output cannot grant memory access.
"""
from __future__ import annotations

import re


DEFAULT_TOPICS = {
    "server": ["服务器", "机器", "主机", "虚拟机", "PVE", "Proxmox", "NAS", "CPU", "内存", "磁盘"],
    "network": ["网络", "内网", "网段", "IP", "地址", "端口", "路由器", "网关", "域名", "DNS", "VPN"],
    "deployment": ["部署", "配置", "容器", "Docker", "服务目录", "systemd", "后端", "Home Assistant", "Hindsight", "Hub"],
}

_READ = "recall_shared_memory"
_WRITE = "remember_shared_memory"
_STATUS = "shared_memory_status"
_DELETE = "forget_shared_memory"
_SHARED = re.compile(r"共享记忆|共享(?:库|记录|文档|事实|偏好)|Hindsight|跨应用|(?:其他|其它|别的)(?:应用|助手)", re.I)
_WRITE_INTENT = re.compile(r"记住|记下|记着|记一下|保存|存入|存到|写入|同步到|别忘|不要忘", re.I)
_DELETE_INTENT = re.compile(r"忘记|忘掉|删除|删掉|清除|移除|取消.{0,8}(?:记忆|记录|文档)")
_STATUS_INTENT = re.compile(r"保存.{0,8}(?:完成|成功|状态|进度)|写入.{0,8}(?:完成|成功|状态|进度)|"
                            r"(?:完成|成功|保存好|存好|写好|处理好|记住)(?:了)?(?:没有|没|了吗|了么|吗|么)|"
                            r"(?:操作|任务).{0,6}(?:状态|进度)|处理(?:到哪|进度)")
_RECALL_INTENT = re.compile(r"查|读|搜|检索|回忆|记得|看看|找|告诉我|有哪些|是什么|有什么|哪|怎么|吗|呢|偏好|记录", re.I)
_SAVED_REFERENCE = re.compile(r"(?:之前|以前|上次|曾经|原来|已经).{0,12}(?:记住|保存|记录|说过|提过)|"
                              r"(?:记住|保存|记录)的(?:内容|信息|事实|偏好|东西)|你(?:还)?记得")
_NO_MEMORY = re.compile(r"(?:不要|别|不用|无需|不必|不需要).{0,6}(?:查|查询|读|读取|搜|搜索|检索|用|使用|访问).{0,5}(?:共享)?记忆|"
                        r"(?:不要|别|不用|无需|不必|不需要)(?:走|使用)?(?:共享)?记忆|"
                        r"(?:不要|别|不用|无需|不必|不需要).{0,6}(?:查|查询|读|读取|访问).{0,5}Hindsight", re.I)
_NO_WRITE = re.compile(r"(?:不要|别|不用|无需|不必|不需要).{0,4}(?:记住|记下|保存|存入|写入|同步)")
_NO_DELETE = re.compile(r"(?:不要|别|不用|无需|不必|不需要).{0,4}(?:忘记|忘掉|删除|删掉|清除|移除)")
_PERSONAL = re.compile(r"我的|我家|我们|我这边|我那边|我自己|咱们|咱家|家里|家中|本地|这台|那台|那几台|这几台")
_PRIVATE_ADDRESS = re.compile(r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
                              r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])")
_NUMBERED_MACHINE = re.compile(r"(?<!\d)\d{1,3}\s*(?:号)?(?:机器|机|服务器)(?!\d)")
_FACT_QUESTION = re.compile(r"[?？]|什么|哪些|哪台|哪里|在哪|多少|几个|几台|有没有|是否|是不是|用了|用的|"
                            r"怎么|怎样|如何|怎么样|状况|情况|状态|配置|部署|地址|端口|版本|目录|"
                            r"介绍|列出|说说|查|看|记得|之前|上次|以前|吗|呢", re.I)
_GENERAL = re.compile(r"什么是|是什么(?:意思|概念|原理)|(?:介绍|解释|科普|学习).{0,8}(?:概念|原理)|"
                       r"(?:想买|购买|选购|推荐|选型|值得买)|怎么(?:学习|入门)|如何(?:学习|入门)")
_FOLLOWUP = re.compile(r"^(?:那台|这台|它|它们|那个|这个|那一个|这一个|另外一台|另一台|还有那台)")
_FOLLOWUP_QUESTION = re.compile(r"呢|吗|[?？]|IP|地址|端口|配置|部署|运行|状态|情况|版本|目录|哪里|哪台|什么|多少", re.I)
_DAILY = re.compile(r"天气|气温|温度|下雨|下雪|带伞|开灯|关灯|开一下灯|关一下灯|打开.{0,5}灯|关闭.{0,5}灯|"
                     r"主灯|台灯|筒灯|亮度|音量|播放音乐|讲笑话|讲故事|你好|晚安|早安")
_LIGHT = re.compile(r"灯|亮度|音量")
_LIGHT_ACTION = re.compile(r"开|关|亮|暗|调|状态|情况|怎么样|多少|吗|呢")
_WEATHER = re.compile(r"天气|气温|温度|下雨|下雪|带伞")
_TECH_DETAIL = re.compile(r"部署|配置|接口|API|服务|插件|后端|地址|端口|版本|目录|IP|Docker|Hindsight", re.I)
_DETAIL_QUESTION = re.compile(r"在哪|哪里|哪台|哪些|什么|多少|怎么|如何|之前|上次|以前")
_DEPLOYED_FACT = re.compile(r"(?:服务器|主机|虚拟机|PVE|Proxmox|NAS).{0,16}(?:部署了什么|部署了哪些|"
                            r"安装了什么|装了什么|运行了什么|上有哪些服务|的(?:IP|地址|端口|版本|目录))", re.I)
_SHARED_META = re.compile(
    r"(?:Hindsight|共享记忆|共享偏好).{0,8}(?:怎么|如何).{0,5}(?:保存|写入|读取|检索|查询|使用|接入|工作|配置(?!的))|"
    r"(?:怎么|如何).{0,8}(?:保存|写入|读取|检索|查询|使用|接入|实现).{0,8}(?:Hindsight|共享记忆|共享偏好)|"
    r"(?:Hindsight|共享记忆).{0,8}(?:有用吗|有用么|好用吗|好用么|有什么用|能做什么|有必要吗|值得用吗)|"
    r"(?:什么是|介绍一下|解释一下).{0,5}(?:Hindsight|共享记忆)|"
    r"(?:要不要|是否需要|需不需要|应不应该|该不该).{0,8}(?:保存|记住|写入)", re.I)


def _clean(text):
    return text.strip()[:2000] if isinstance(text, str) else ""


def _settings(config):
    if not isinstance(config, dict):
        return {}
    value = config.get("hindsight") or {}
    return value if isinstance(value, dict) else {}


def _keywords(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value[:100] if isinstance(item, str) and 0 < len(item.strip()) <= 80]


def _contains(text, word):
    # ASCII acronyms such as IP must not match arbitrary words ("ship").
    if word.isascii():
        return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])", text, re.I))
    return word.casefold() in text.casefold()


def _topic(text, config):
    override = _settings(config).get("recall_topics")
    topics = override if isinstance(override, dict) else DEFAULT_TOPICS
    for topic, words in topics.items():
        if isinstance(topic, str) and any(_contains(text, word) for word in _keywords(words)):
            return topic
    return ""


def _personal(text, config):
    aliases = _keywords(_settings(config).get("asset_aliases", []))
    return bool(_PERSONAL.search(text) or _PRIVATE_ADDRESS.search(text) or _NUMBERED_MACHINE.search(text)
                or any(_contains(text, alias) for alias in aliases))


def _explicit_shared_write(text):
    if not _SHARED.search(text) or _NO_WRITE.search(text):
        return False
    action_text = re.split(r"[：:\n]", text, maxsplit=1)[0]
    if _STATUS_INTENT.search(action_text) or _DELETE_INTENT.search(action_text) or _SHARED_META.search(action_text):
        return False
    if re.match(r"^(?:请帮我|请|帮我|给我)?(?:查|读|搜|找|检索|回忆|看看|告诉我|你(?:还)?记得)", action_text):
        return False
    # Looking up previously saved material is a read, not another retain.
    if (_SAVED_REFERENCE.search(action_text) or re.search(r"(?:保存|记住|记下|写入)的", action_text)) and re.search(r"查|读|搜|找|检索|回忆", action_text):
        return False
    return bool(_WRITE_INTENT.search(action_text))


def _previous_shared(text):
    """An immediately preceding explicit shared request, never an inherited route."""
    return bool(text and not _NO_MEMORY.search(text) and not _SHARED_META.search(text) and _SHARED.search(text)
                and (_RECALL_INTENT.search(text) or _WRITE_INTENT.search(text) or _DELETE_INTENT.search(text)))


def _shared_delete(text, previous_user):
    return bool(_DELETE_INTENT.search(text) and not _NO_DELETE.search(text)
                and (_SHARED.search(text) or (_previous_shared(previous_user)
                     and re.search(r"刚才|这条|那条|这份|那份|这个|那个|它|记录|文档|记忆", text))))


def _shared_status(text, previous_user):
    return bool(_STATUS_INTENT.search(text) and (_SHARED.search(text) or _explicit_shared_write(previous_user)))


def _decision(allowed, topic="", reason="unrelated"):
    return {"allowed": bool(allowed), "topic": topic, "reason": reason}


def _environment_clauses(text):
    """Keep infrastructure questions, not incidental server nouns in lamp requests."""
    clauses = re.split(r"[，,。；;!?！？\n]|然后|并且|顺便|再告诉我|再查一下|再查", text)
    remaining = []
    for clause in clauses:
        technical_question = _TECH_DETAIL.search(clause) and _DETAIL_QUESTION.search(clause)
        if _LIGHT.search(clause) and _LIGHT_ACTION.search(clause) and not technical_question:
            continue
        if _WEATHER.search(clause) and not technical_question:
            continue
        remaining.append(clause)
    return "，".join(remaining)


def recall_decision(text, config, previous_user=""):
    """Allow shared recall for explicit memory requests or personal environment facts.

    ``recall_topics`` may replace the default index. ``asset_aliases`` are names
    of known personal machines/services; never put passwords or tokens in it.
    This is relevance routing, not an authentication or live-status check.
    """
    text, previous_user = _clean(text), _clean(previous_user)
    if not text or _NO_MEMORY.search(text):
        return _decision(False, reason="empty_or_opt_out")
    if _SHARED.search(text) and _SHARED_META.search(re.split(r"[：:\n]", text, maxsplit=1)[0]):
        return _decision(False, reason="general_question")
    if _shared_delete(text, previous_user):
        return _decision(True, "shared", "resolve_explicit_delete")
    # Saving/status does not need a speculative read; their own tools are exposed below.
    if _explicit_shared_write(text) or _shared_status(text, previous_user):
        return _decision(False, reason="shared_management")
    if (_SHARED.search(text) or _SAVED_REFERENCE.search(text)) and _RECALL_INTENT.search(text):
        if not _GENERAL.search(text):
            return _decision(True, "shared", "explicit_recall")

    environment = _environment_clauses(text)
    topic = _topic(environment, config)
    personal = _personal(environment, config) or bool(_DEPLOYED_FACT.search(environment))
    if topic and personal and _FACT_QUESTION.search(environment):
        # "我想买服务器" is not a request for saved information. A comparison that
        # explicitly asks about an existing setup may still use its saved facts.
        if _GENERAL.search(environment) and not re.search(r"(?:现有|已有|之前|现在用|目前用|已部署|已安装)", environment):
            return _decision(False, reason="general_question")
        return _decision(True, topic, "personal_environment")

    # A pure daily request cannot revive a server topic from earlier dialogue.
    if _DAILY.search(text):
        return _decision(False, reason="daily_request")
    if len(text) <= 60 and _FOLLOWUP.search(text) and _FOLLOWUP_QUESTION.search(text):
        previous = recall_decision(previous_user, config)  # deliberately no older context
        if previous["allowed"] or _previous_shared(previous_user):
            return _decision(True, previous["topic"] or "shared", "immediate_followup")
    return _decision(False, reason="general_question" if topic else "unrelated")


def shared_tools_for_turn(text, config, previous_user=""):
    """Return only relevant shared tool names; callers must also enforce identity."""
    text, previous_user = _clean(text), _clean(previous_user)
    if not text or _NO_MEMORY.search(text):
        return frozenset()
    if _SHARED.search(text) and _SHARED_META.search(re.split(r"[：:\n]", text, maxsplit=1)[0]):
        return frozenset()
    names = set()
    if recall_decision(text, config, previous_user)["allowed"]:
        names.add(_READ)
    if _explicit_shared_write(text):
        names.update((_WRITE, _STATUS))
    if _shared_status(text, previous_user):
        names.add(_STATUS)
    if _shared_delete(text, previous_user):
        names.update((_READ, _DELETE, _STATUS))
    return frozenset(names)


__all__ = ["DEFAULT_TOPICS", "recall_decision", "shared_tools_for_turn"]
