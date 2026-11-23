import asyncio
from types import SimpleNamespace

import httpx
import aiohttp
from aiohttp import web

from core.agent import ha_commands
from core.api.panel_controls import describe
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
        rows.append(describe(config, device, data, device.room))
    return {'devices': rows}


async def set_device(config, body):
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text='设备开关参数无效')
    entity = body.get('id')
    if not all(ha_commands.allowed(config, entity, write=write) for write in (False, True)):
        raise web.HTTPForbidden(text='未授权操作这个设备')
    ha = ha_config(config)
    device = next((item for item in ha_commands.devices(ha) if item.entity_id == entity), None)
    if device is None:
        raise web.HTTPForbidden(text='该设备不支持面板控制')
    if set(body) == {'id', 'on'} and isinstance(body['on'], bool):
        command = ha_commands.Command(device, 'turn_on' if body['on'] else 'turn_off')
    elif (set(body) == {'id', 'action', 'value'} and type(body['value']) is int and 0 <= body['value'] <= 100
          and (device.domain, body['action']) in (('light', 'brightness'), ('media_player', 'volume'))):
        command = ha_commands.Command(device, body['action'], body['value'])
    else:
        raise web.HTTPBadRequest(text='设备控制参数无效')
    result = await ha_commands.execute(command, ha)
    return {'confirmed': result.confirmed, 'message': str(result)}


def register(app, admin):
    from core.admin import speaker_cards

    async def manage(request):
        admin._require_login(request)
        path=admin.extensions_path.with_name('speaker-layout.db')
        try:
            config=await admin._blocking(admin._merged)
            cards=await asyncio.wait_for(speaker_cards.catalog(config),12)
            if request.method=='POST':
                body=await admin._read_json(request); selected=body.get('selected')
                if not isinstance(selected,list) or any(not isinstance(key,str) or key not in {c['id'] for c in cards} for key in selected):
                    return admin._error('卡片不存在，请刷新清单',400)
                rev,selected=await admin._blocking(speaker_cards.layout,path,selected,body.get('revision'))
                links=tuple(admin.speaker_preview.links.values())
                await asyncio.gather(*(asyncio.wait_for(link.ws.send_json({'op':'layout_changed'}),1) for link in links),return_exceptions=True)
            else: rev,selected=await admin._blocking(speaker_cards.layout,path)
            return admin._json({'cards':cards,'revision':rev,'selected':selected if selected is not None else speaker_cards.default_ids(cards)})
        except ValueError as error: return admin._error(str(error),409)
        except (OSError, asyncio.TimeoutError, aiohttp.ClientError): return admin._error('家庭服务暂不可用，未修改布局',502)

    app.router.add_get('/admin/api/speaker/cards',manage)
    app.router.add_post('/admin/api/speaker/cards',manage)
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
                _,selected=await asyncio.to_thread(speaker_cards.layout,admin.extensions_path.with_name('speaker-layout.db'))
                if selected == []: result={'devices':[]}
                else:
                    try: cards=await asyncio.wait_for(speaker_cards.catalog(config),12)
                    except (ValueError,OSError,aiohttp.ClientError,asyncio.TimeoutError):
                        if selected is not None: raise
                        result=await devices_panel(config)
                    else:
                        order=selected if selected is not None else speaker_cards.default_ids(cards)
                        by_id={c['id']:c for c in cards}
                        result={'devices':[by_id[key] for key in order if key in by_id]}
        except web.HTTPException as error:
            return admin._error(error.text, error.status)
        except (aiohttp.ClientError,httpx.HTTPError, asyncio.TimeoutError, weather.WeatherError, KeyError, ValueError):
            return admin._error('天气服务暂不可用' if request.path.endswith('/weather') else '无法读取设备状态，请检查家庭服务', 502)
        return web.json_response(result, headers={'Cache-Control': 'no-store'})

    app.router.add_get('/display/weather', panel)
    app.router.add_get('/display/devices', panel)
    app.router.add_post('/display/devices', panel)
    app.router.add_post('/display/agenda-asr', panel)
