"""Exact simple weather questions; compound, contextual and advisory requests stay with the LLM."""
import asyncio
import math
import re
from dataclasses import dataclass
from datetime import timedelta

from plugins_func.functions import get_weather as weather

# Bare names are intentionally limited. Explicit city/district names are accepted separately.
CITIES = set('北京 上海 天津 重庆 大连 沈阳 长春 哈尔滨 广州 深圳 珠海 东莞 佛山 惠州 杭州 宁波 温州 绍兴 南京 苏州 无锡 常州 扬州 成都 武汉 长沙 西安 郑州 济南 青岛 烟台 威海 厦门 福州 泉州 南昌 合肥 昆明 贵阳 南宁 海口 三亚 兰州 西宁 银川 乌鲁木齐 拉萨 呼和浩特 石家庄 太原 香港 澳门 台北 东京 大阪 首尔 新加坡 伦敦 巴黎 纽约'.split())
LOCAL = {'', '本地', '当地', '这里', '这儿', '我这里', '我这儿', '我们这里'}
DAYS = {'今天': 0, '今日': 0, '明天': 1, '明日': 1, '后天': 2, '现在': 0, '当前': 0}
TOPICS = {
    'weather': r'(?:天气(?:预报)?(?:怎么样|如何|怎样|情况|好不好|好吗)?|什么天气)',
    'temperature': r'(?:(?:气温|温度)(?:是多少度|多少度|是多少|多少|怎么样|如何)?|多少度|几度)',
    'rain': r'(?:会不会下雨|会下雨吗|下不下雨|下雨吗|有没有雨|有雨吗|是否下雨|要不要带伞|需要带伞吗)',
    'snow': r'(?:会不会下雪|会下雪吗|下不下雪|下雪吗|有没有雪|有雪吗|是否下雪)',
}


@dataclass(frozen=True)
class Query:
    location: str
    day: int
    kind: str
    current: bool = False


def parse(text):
    if not isinstance(text, str) or len(text) > 80: return None
    text = re.sub(r'\s+', '', text).strip('，,。.!！?？')
    text = re.sub(r'^(?:小葵小葵|小葵)[，,]?', '', text)
    text = re.sub(r'^(?:请问|请)?(?:帮我|给我)?(?:查询一下|查一下|查一查|查查|查询|看看|看一下|告诉我|查)?', '', text)
    # Only surrounding punctuation is removed. Internal compound clauses must never become one command.
    text = re.sub(r'[呢啊呀吧]$', '', text).rstrip('，,。.!！?？')
    if not text or re.search(r'[，,。.!！?？;；:：/\\]|如果|然后|并且|顺便|为什么|昨天|今晚|明晚|上午|下午|晚上|周末|下周|这周|小时|几点|对比|比较|或者|还是|别查|别说|不想|不用', text):
        return None
    if text.startswith(('不要', '不查', '不问')): return None
    for kind, topic in TOPICS.items():
        found = re.fullmatch(r'(.*?)(' + topic + r')', text)
        if not found: continue
        prefix = found.group(1)
        # "今天的天气会下雨吗" is a simple precipitation request.
        if kind in ('rain', 'snow'):
            prefix = re.sub(r'(?:的)?天气$', '', prefix)
        prefix = prefix.removesuffix('的')
        day, current = 0, False
        dates = re.findall('|'.join(DAYS), prefix)
        if len(dates) > 1: return None
        if dates:
            token = dates[0]; day = DAYS[token]; current = token in ('现在', '当前')
            if prefix.startswith(token): prefix = prefix[len(token):]
            elif prefix.endswith(token): prefix = prefix[:-len(token)]
            else: return None
        prefix = prefix.removesuffix('的')
        if prefix in LOCAL: return Query('', day, kind, current)
        if prefix in CITIES: return Query(prefix, day, kind, current)
        if sum(city in prefix for city in CITIES) > 1: return None
        # Restrict unknown names to explicit administrative areas; reject conversational fragments.
        if re.fullmatch(r'[\u4e00-\u9fff]{1,10}(?:市|区|县)', prefix) and not re.search(r'你|我|他|她|它|这|那|哪个|哪里|什么|和|与|及|都|能|知道|想|觉得|说|听|故事|介绍|解释|好像|感觉|省', prefix):
            return Query(prefix, day, kind, current)
    return None


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _temp(value):
    if not _number(value): raise weather.WeatherError('这次没有查到完整的气温数据')
    return ('零下' + str(abs(round(value))) if value < -0.5 else str(round(value))) + '度'


def reply(query, place, forecast):
    today = weather.local_date(forecast)
    target = (today + timedelta(days=query.day)).isoformat()
    daily = forecast.get('daily') or {}
    try: index = daily['time'].index(target)
    except (KeyError, ValueError, AttributeError): raise weather.WeatherError('这次没有查到所需日期的预报')
    def value(key):
        rows = daily.get(key) or []
        return rows[index] if index < len(rows) else None
    label = ('今天', '明天', '后天')[query.day]
    name = place.name.split('（')[0]
    code = value('weather_code')
    low, high = value('temperature_2m_min'), value('temperature_2m_max')
    current = forecast.get('current') or {}
    if query.kind == 'temperature':
        if query.current:
            return f'{name}现在{_temp(current.get("temperature_2m"))}。'
        return f'{name}{label}气温{_temp(low)}到{_temp(high)}。'
    if query.kind == 'weather':
        if query.current:
            current_code = current.get('weather_code')
            if current_code not in weather.WMO: raise weather.WeatherError('这次没有查到当前天气')
            return f'{name}现在{weather.WMO[current_code]}，{_temp(current.get("temperature_2m"))}。'
        if code not in weather.WMO: raise weather.WeatherError('这次没有查到完整的天气预报')
        summary = weather._day_weather(code, value('precipitation_sum'))
        return f'{name}{label}{summary}。气温{_temp(low)}到{_temp(high)}。'
    codes = weather.SNOW_CODES if query.kind == 'snow' else weather.RAIN_CODES | {95, 96, 99}
    what = '下雪' if query.kind == 'snow' else '下雨'
    if query.current:
        current_code = current.get('weather_code')
        if current_code not in weather.WMO: raise weather.WeatherError('这次没有查到当前天气')
        state = f'正在{what}' if current_code in codes else weather.WMO[current_code]
        return f'{name}当前天气是{state}。'
    probability = value('precipitation_probability_max')
    if code not in weather.WMO: raise weather.WeatherError('这次没有查到完整的降水预报')
    chance = f'降水概率最高{round(probability)}%。' if _number(probability) and 0 <= probability <= 100 else ''
    if code in codes:
        return f'{name}{label}预报有{weather.WMO[code]}。{chance}'
    if query.kind == 'rain' and code in weather.SNOW_CODES:
        return f'{name}{label}预报有{weather.WMO[code]}，出门注意防滑。'
    if query.kind == 'rain' and _number(probability) and probability >= 40:
        return f'{name}{label}有降水可能。{chance}建议带伞。'
    return f'{name}{label}预报没有明显{what}迹象。{chance}'


async def execute(conn, query, stale=lambda: False):
    async def lookup():
        location = query.location or await asyncio.to_thread(weather.local_query, conn)
        place = await weather.find_place(location)
        if stale(): return None
        forecast = await weather.fetch_forecast(place)
        return None if stale() else reply(query, place, forecast)
    if stale(): return None
    return await asyncio.wait_for(lookup(), timeout=12)
