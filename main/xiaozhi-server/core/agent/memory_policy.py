"""Conservative retention rules. Only clearly disposable turns expire the next local day."""
import json
import re
from datetime import datetime, time as clock, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
TEMPORARY = {'weather', 'device', 'smalltalk'}
LABELS = {'weather': '天气查询', 'device': '设备操作', 'smalltalk': '简单寒暄', 'keep': '保留对话'}
SAFE_TOOLS = {'get_weather', 'hass_get_state', 'hass_set_state'}
DURABLE = re.compile(
    r'记住|记下|记着|别忘|不要忘|忘掉|忘记|删除记忆|清除记忆|以后|今后|每次|每天|每晚|每周|每月|习惯|偏好|'
    r'喜欢|不喜欢|讨厌|默认|总是|一直|通常|一般会|经常|长期|定时|提醒|闹钟|倒计时|约定|待办|计划|'
    r'明天.*(?:点|时)|下周|下个月|项目|进度|故障|坏了|失灵|报错|修理|修复|不能|失败|问题|'
    r'我叫|我住|住在|我是|我的名字|过敏|生日|职业|工作|家人|孩子|妻子|丈夫|父母|爸爸|妈妈|不想|不希望|希望')
SMALLTALK = re.compile(r'(?:你好|您好|嗨|哈喽|早上好|早安|晚上好|晚安|谢谢|谢谢你|好的|好|嗯|嗯嗯|'
                      r'小葵|小葵小葵|哈喽小葵|小葵同学|再见|拜拜|退下|结束|不聊了|'
                      r'(?:对)?你在(?:干吗|干嘛|做什么)(?:呢|呀|啊)?|我要睡觉(?:了|啊)?|在吗)')


def text_of(value):
    text = value if isinstance(value, str) else ''
    if text.lstrip().startswith('{'):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict): text = str(parsed.get('content', text))
        except (ValueError, TypeError): pass
    return text.strip()


def classify(text, config, tools=()):
    text = text_of(text)
    if not text or len(text) > 120 or DURABLE.search(text): return 'keep'
    clean = re.sub(r'^(?:(?:uh|um|呃|嗯)[，,\s]*)+', '', text, flags=re.I)
    compact = re.sub(r'[\s，,。.!！?？、；;]', '', clean)
    if SMALLTALK.fullmatch(compact): return 'smalltalk'
    from core.agent import weather_commands, ha_commands
    if weather_commands.parse(clean) is not None: return 'weather'
    ha = ((config or {}).get('plugins') or {}).get('home_assistant') or {}
    if ha_commands.parse(clean, ha) is not None: return 'device'
    # Older turns predate the direct hooks. Tool evidence only applies to short immediate requests.
    if tools and set(tools) <= SAFE_TOOLS and len(compact) <= 35:
        if re.search(r'如果|要是|为什么|怎么|如何|不对|不行|不亮|出错|之后|以前|昨天|明天|后天', compact):
            return 'keep'
        if set(tools) == {'get_weather'} and re.fullmatch(r'(?:帮我|请|查一下|查询|看看|今天|现在|当地|本地|的|天气|怎么样|如何|多少度|下雨|吗|呢|啊|呀)+', compact):
            return 'weather'
        if set(tools) <= {'hass_get_state', 'hass_set_state'}:
            aliases = {alias for device in ha_commands.devices(ha) for alias in device.aliases}
            if any(alias in compact for alias in aliases) and re.match(r'^(?:请|帮我|把|给我|开|关|打|停|调|观|查)', compact):
                return 'device'
    # Multiple immediate targets, all of them configured; no model inference needed.
    action = re.fullmatch(r'(?:请)?(?:帮我)?(?:把)?(打开|关闭|关掉|开启|关|开)(.+?)(?:了|吧)?', compact)
    if action:
        # Match against the original remainder, preserving target separators.
        match = re.match(r'^(?:请)?(?:帮我)?(?:把)?(?:打开|关闭|关掉|开启|关|开)(.+?)(?:了|吧)?[。.!！?？]*$', clean)
        if match:
            targets = re.split(r'[、，,]|和|以及', match.group(1))
            aliases = {alias for device in ha_commands.devices(ha) for alias in device.aliases}
            if targets and all(ha_commands.normalize(p) in aliases for p in targets): return 'device'
    return 'keep'


def expires_at(created):
    tomorrow = datetime.fromtimestamp(created, ZONE).date() + timedelta(days=1)
    return datetime.combine(tomorrow, clock.min, ZONE).timestamp()


def group_turns(rows):
    group = []
    for row in rows:
        if row['role'] == 'user' and group:
            yield group
            group = []
        group.append(row)
    if group: yield group


def turn_policy(rows, config):
    user = next((r for r in rows if r['role'] == 'user'), None)
    if user is None:
        # A standalone wake greeting contains no user facts.
        greetings = {'我在', '我在这里哦！', '我在这里哦', '您请说，我正听着。'}
        return ('smalltalk', expires_at(rows[0]['created'])) if all(r['content'] in greetings for r in rows) else ('keep', None)
    tools = [((call.get('function') or {}).get('name') or '') for r in rows for call in (r.get('tool_calls') or []) if isinstance(call, dict)]
    kind = classify(user['content'], config, tools)
    # Do not expire a turn involving tasks or other tools just because its first words are simple.
    if tools and not set(tools) <= SAFE_TOOLS: kind = 'keep'
    return kind, expires_at(user['created']) if kind in TEMPORARY else None
