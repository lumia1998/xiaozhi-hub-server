"""Simple weather questions -> existing Open-Meteo service -> TTS, with zero LLM calls."""
import asyncio
from core.agent import weather_commands
from plugins_func.functions.get_weather import WeatherError

NAME = 'weather'
ERROR_REPLY = '天气暂时没查到，请稍后再试。'


def match(conn, text):
    config = conn.config
    intent = (config.get('Intent') or {}).get((config.get('selected_module') or {}).get('Intent')) or {}
    # Honour the existing weather plugin checkbox and an optional explicit fast-path opt-out.
    if 'get_weather' not in intent.get('functions', []): return None
    if ((config.get('plugins') or {}).get('get_weather') or {}).get('direct_weather', True) is False: return None
    return weather_commands.parse(text)


async def intercept(conn, text, stale=lambda: False):
    query = match(conn, text)
    if query is None: return {'handled': False}
    confirmed = False
    try:
        result = await weather_commands.execute(conn, query, stale)
        confirmed = result is not None
    except WeatherError as exc:
        result = f'天气暂时没查到，{exc}。'
    except (asyncio.TimeoutError, Exception) as exc:
        conn.logger.warning(f'weather direct query failed: {type(exc).__name__}')
        result = ERROR_REPLY
    if stale() or result is None:
        return {'handled': True, 'reply': None, 'cancelled': True}
    return {'handled': True, 'reply': result, 'action': query.kind,
            'target': query.location or '本地', 'confirmed': confirmed}
