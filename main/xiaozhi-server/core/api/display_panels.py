import asyncio
from types import SimpleNamespace

import httpx
from aiohttp import web

from core.agent import ha_commands
from plugins_func.functions import get_weather as weather


async def weather_panel(config, remote):
    place = await weather.find_place(await asyncio.to_thread(weather.local_query, SimpleNamespace(config=config, client_ip=remote)))
    forecast = await weather.fetch_forecast(place)
    current = forecast.get('current') or {}
    daily = forecast.get('daily') or {}
    days = []
    for index, day in enumerate(daily.get('time', [])[:7]):
        def value(key):
            values = daily.get(key) or []
            return values[index] if index < len(values) else None
        days.append({'date': day, 'condition': weather.WMO.get(value('weather_code'), '未知'),
                     'low': value('temperature_2m_min'), 'high': value('temperature_2m_max')})
    return {'place': place.name, 'temperature': current.get('temperature_2m'), 'humidity': current.get('relative_humidity_2m'),
            'condition': weather.WMO.get(current.get('weather_code'), '未知'), 'time': current.get('time'), 'days': days}


def ha_config(config):
    plugins = config.get('plugins') or {}
    return plugins.get('home_assistant') or plugins.get('hass_state') or {}


async def devices_panel(config):
    ha = ha_config(config)
    devices = [device for device in ha_commands.devices(ha) if ha_commands.allowed(config, device.entity_id, write=False)]
    if not devices:
        return {'devices': []}
    async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
        response = await client.get(ha['base_url'].rstrip('/') + '/api/states', headers={'Authorization': 'Bearer ' + ha['api_key']})
        response.raise_for_status()
        states = {row['entity_id']: row for row in response.json()}
    rows = []
    for device in devices:
        data = states.get(device.entity_id, {})
        state = data.get('state')
        on = True if state in ('on', 'playing', 'paused', 'idle') else False if state in ('off', 'standby') else None
        capable = device.domain != 'media_player' or bool((data.get('attributes', {}).get('supported_features') or 0) & (128 if on else 256))
        rows.append({'id': device.entity_id, 'room': device.room, 'name': device.name, 'on': on,
                     'writable': on is not None and capable and ha_commands.allowed(config, device.entity_id, write=True)})
    return {'devices': rows}


async def set_device(config, body):
    if not isinstance(body, dict) or not isinstance(body.get('on'), bool):
        raise web.HTTPBadRequest(text='设备开关参数无效')
    entity = body.get('id')
    if not all(ha_commands.allowed(config, entity, write=write) for write in (False, True)):
        raise web.HTTPForbidden(text='未授权操作这个设备')
    ha = ha_config(config)
    device = next((item for item in ha_commands.devices(ha) if item.entity_id == entity), None)
    if device is None:
        raise web.HTTPForbidden(text='该设备不支持面板控制')
    result = await ha_commands.execute(ha_commands.Command(device, 'turn_on' if body['on'] else 'turn_off'), ha)
    return {'confirmed': result.confirmed, 'message': str(result)}


def register(app, admin):
    async def panel(request):
        store = admin._pairing_store()
        if not store or not await asyncio.to_thread(store.verify, request.headers.get('Device-Id'), request.headers.get('X-Device-Pairing-Token')):
            return admin._error('请先在音箱设置中配置有效的配对令牌', 403)
        if request.path.endswith('/agenda-asr'):
            if request.content_type.lower() != 'audio/l16':
                return admin._error('需要单声道 16kHz PCM 录音', 400)
            return await admin.transcribe_pcm(request, getattr(admin.ws_server, '_asr', None))
        config = await admin._blocking(admin._merged)
        try:
            if request.method == 'POST':
                result = await set_device(config, await request.json())
            elif request.path.endswith('/weather'):
                result = await asyncio.wait_for(weather_panel(config, request.remote), timeout=15)
            else:
                result = await devices_panel(config)
        except web.HTTPException as error:
            return admin._error(error.text, error.status)
        except (httpx.HTTPError, asyncio.TimeoutError, weather.WeatherError, KeyError, ValueError):
            return admin._error('天气服务暂不可用' if request.path.endswith('/weather') else '无法读取设备状态，请检查家庭服务', 502)
        return web.json_response(result, headers={'Cache-Control': 'no-store'})

    app.router.add_get('/display/weather', panel)
    app.router.add_get('/display/devices', panel)
    app.router.add_post('/display/devices', panel)
    app.router.add_post('/display/agenda-asr', panel)
