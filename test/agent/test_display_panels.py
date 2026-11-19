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
