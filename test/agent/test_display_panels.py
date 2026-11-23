import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.api import display_panels as panels


def configuration():
    return {'selected_module': {'Intent': 'function_call'}, 'Intent': {'function_call': {'functions': ['hass_state']}},
            'plugins': {'home_assistant': {'base_url': 'http://ha.test', 'api_key': 'fixture',
                      'devices': ['主卧,筒灯,switch.downlight', '主卧,台灯,light.lamp']}}}


@pytest.mark.asyncio
async def test_cards_match_allowlist_and_require_positive_permission(monkeypatch):
    config = configuration()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200,json=[
        {'entity_id':'switch.downlight','state':'on'}, {'entity_id':'switch.other','state':'on'}])))
    monkeypatch.setattr(panels.httpx, 'AsyncClient', lambda **_:client)
    rows = (await panels.devices_panel(config))['devices']
    assert [(row['id'],row['on'],row['writable']) for row in rows] == [('switch.downlight', True, True),('light.lamp', None, False)]
    execute = AsyncMock(return_value=panels.ha_commands.ExecutionResult('状态尚未更新',status='submitted'))
    monkeypatch.setattr(panels.ha_commands, 'execute', execute)
    assert not (await panels.set_device(config, {'id':'switch.downlight','on':False}))['confirmed']
    assert execute.await_args.args[0].action == 'turn_off'
    with pytest.raises(web.HTTPForbidden): await panels.set_device(config, {'id':'switch.other','on':True})
    config['Intent']['function_call']['functions'] = ['hass_get_state']
    with pytest.raises(web.HTTPForbidden): await panels.set_device(config, {'id':'switch.downlight','on':True})
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_light_slider_uses_verified_command_and_write_permission(monkeypatch):
    config = configuration()
    lamp = panels.ha_commands.devices(panels.ha_config(config))[1]
    row = {'state':'on', 'attributes':{'supported_color_modes':['brightness'], 'brightness':128}}
    assert panels.describe(config, lamp, row, lamp.room)['level'] == {'kind':'brightness','value':50,'writable':True}
    execute = AsyncMock(return_value=panels.ha_commands.ExecutionResult('设备已确认', confirmed=True))
    monkeypatch.setattr(panels.ha_commands, 'execute', execute)
    payload = {'id':lamp.entity_id, 'action':'brightness', 'value':63}
    assert (await panels.set_device(config, payload))['confirmed']
    assert execute.await_args.args[0] == panels.ha_commands.Command(lamp, 'brightness', 63)
    for invalid in ({'value':True}, {'value':101}, {'action':'volume'}, {'on':False}):
        with pytest.raises(web.HTTPBadRequest): await panels.set_device(config, {**payload, **invalid})
    config['Intent']['function_call']['functions'] = ['hass_get_state']
    with pytest.raises(web.HTTPForbidden): await panels.set_device(config, payload)
    assert not panels.describe(config, lamp, row, lamp.room)['level']['writable']
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_display_routes_require_pairing():
    app = web.Application()
    admin = SimpleNamespace(_pairing_store=lambda:Mock(verify=Mock(return_value=False)),
                            _error=lambda text,code:web.json_response({'error':text},status=code))
    panels.register(app,admin)
    async with TestClient(TestServer(app)) as client:
        for path in ('weather','devices'):
            assert (await client.get('/display/'+path)).status == 403
        assert (await client.post('/display/devices',json={'on':True})).status == 403
        assert (await client.post('/display/agenda-asr',data=b'12')).status == 403


@pytest.mark.asyncio
async def test_weather_panel_handles_missing_daily_fields(monkeypatch):
    monkeypatch.setattr(panels.weather,'local_query',lambda _: '大连')
    monkeypatch.setattr(panels.weather,'find_place',AsyncMock(return_value=SimpleNamespace(name='大连')))
    monkeypatch.setattr(panels.weather,'fetch_forecast',AsyncMock(return_value={'current':{'temperature_2m':0,'weather_code':0},
                        'daily':{'time':['2026-11-19'],'temperature_2m_min':[-2]}}))
    data = await panels.weather_panel({},None)
    assert data['temperature'] == 0 and data['days'][0]['low'] == -2 and data['days'][0]['high'] is None


@pytest.mark.asyncio
async def test_speaker_cards_layout_preserves_custom_order(tmp_path, monkeypatch):
    from core.admin import speaker_cards

    cards = [
        {'id': 'dev-a', 'room': '主卧', 'name': '台灯', 'controls': [{'id': 'light.a', 'name': '台灯', 'on': False, 'writable': True}], 'status': '有可用实体'},
        {'id': 'dev-b', 'room': '主卧', 'name': '筒灯', 'controls': [{'id': 'light.b', 'name': '筒灯', 'on': True, 'writable': True}], 'status': '有可用实体'},
        {'id': 'dev-c', 'room': '主卧', 'name': '温湿度计', 'controls': [], 'status': '22°C / 45%'},
    ]
    layout_db = tmp_path / 'speaker-layout.db'
    speaker_cards.layout(layout_db, ['dev-c', 'dev-a'], 0)
    monkeypatch.setattr(speaker_cards, 'catalog', AsyncMock(return_value=cards))

    app = web.Application()
    admin = SimpleNamespace(
        extensions_path=tmp_path / 'extensions.json',
        _pairing_store=lambda: Mock(verify=Mock(return_value=True)),
        _blocking=lambda fn, *a: asyncio.to_thread(fn, *a),
        _merged=lambda: {},
        _error=lambda text, code: web.json_response({'error': text}, status=code),
    )
    panels.register(app, admin)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get('/display/devices', headers={'Device-Id': 'sp1', 'X-Device-Pairing-Token': 'tok'})
        assert resp.status == 200
        body = await resp.json()
        assert [item['id'] for item in body['devices']] == ['dev-c', 'dev-a']
