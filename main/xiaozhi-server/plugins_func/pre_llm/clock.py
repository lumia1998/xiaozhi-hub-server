import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


NAME = 'clock'
ERROR_REPLY = '暂时无法读取当前时间。'
_QUESTIONS = {'现在几点': 'time', '几点了': 'time', '现在几点了': 'time', '现在几点钟': 'time',
              '现在是什么时间': 'time', '今天几号': 'date', '今天是几号': 'date', '今天几月几号': 'date',
              '今天星期几': 'weekday', '今天是星期几': 'weekday', '今天周几': 'weekday'}


def match(conn, text):
    if not isinstance(text, str) or not conn.config.get('direct_clock', True):
        return None
    return _QUESTIONS.get(re.sub(r'^请问[，, ]*', '', text.strip().rstrip('？?！!。 ')))


async def intercept(conn, text, stale):
    kind = match(conn, text)
    if not kind:
        return {'handled': False}
    zone_name = (getattr(conn, 'features', {}) or {}).get('timezone')
    try:
        zone = ZoneInfo(zone_name) if isinstance(zone_name, str) else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    if zone is None:
        offset = (conn.config.get('server') or {}).get('timezone_offset', 8)
        zone = timezone(timedelta(hours=float(offset)))
    now = datetime.now(zone)
    reply = {'time': f'现在是{now.hour}点{now.minute:02d}分。', 'date': f'今天是{now.month}月{now.day}日。',
             'weekday': '今天是星期' + '一二三四五六日'[now.weekday()] + '。'}[kind]
    return {'handled': True, 'reply': reply, 'record_operation': False}
