import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer

from core.admin import speaker_cards, connections
from core.admin.admin_handler import AdminHandler


def test_visibility_is_only_layout(tmp_path):
    path=tmp_path/'cards.db'
    assert speaker_cards.layout(path)==(0,None)
    assert speaker_cards.layout(path,[],0)==(1,[])
    with pytest.raises(ValueError): speaker_cards.layout(path,['x'],0)
    assert speaker_cards.layout(path,['x','x'],1)==(2,['x'])


@pytest.mark.asyncio
async def test_paired_device_cannot_read_or_switch_another_device(tmp_path,monkeypatch):
    admin=AdminHandler({},SimpleNamespace(pairing=SimpleNamespace(verify=lambda d,t:(d,t)==('a','test-only'))),tmp_path/'.config.yaml')
    monkeypatch.setattr(admin,'_from_this_machine',lambda req: False)
    app=web.Application();admin.register(app)
    async with TestClient(TestServer(app)) as client:
        headers={'Device-Id':'a','X-Device-Pairing-Token':'test-only'}
        assert (await client.get('/kiosk/chat/history?device=b',headers=headers)).status==403
        assert (await client.post('/kiosk/llm/use',json={'device':'b','entry':'x'},headers=headers)).status==403
        assert (await client.get('/kiosk/llm?device=b',headers=headers)).status==403


@pytest.mark.asyncio
async def test_paired_console_presence_and_rpc(tmp_path,monkeypatch):
    store=SimpleNamespace(verify=lambda device,token: (device,token)==('speaker','test-only'))
    admin=AdminHandler({},SimpleNamespace(pairing=store),tmp_path/'.config.yaml')
    monkeypatch.setattr(admin,'_logged_in',lambda request: request.headers.get('Test-Admin')=='yes')
    app=web.Application();admin.register(app)
    async with TestClient(TestServer(app)) as client:
        url='/admin/speaker/speaker/api/history'; headers={'Test-Admin':'yes'}
        assert (await client.get('/display/console')).status==403
        assert (await client.get(url)).status==401
        assert (await client.get(url,headers=headers)).status==503
        async with client.ws_connect('/display/console',headers={'Device-Id':'speaker','X-Device-Pairing-Token':'test-only'}) as ws:
            async def devices():return (await (await client.get('/admin/api/devices',headers=headers)).json())['devices']
            assert any(d.get('standby') and d['device_id']=='speaker' for d in await devices())
            active=SimpleNamespace(session_id='test-voice',device_id='speaker')
            connections.register(active)
            try: assert len([d for d in await devices() if d['device_id']=='speaker'])==1
            finally: connections.unregister(active)
            assert (await client.post(url,headers=headers)).status==403
            assert (await client.post('/admin/speaker/speaker/api/power-off',headers={**headers,'X-Speaker-Console':'1'})).status==403
            task=asyncio.create_task(client.get(url,headers=headers)); msg=await ws.receive_json(timeout=3)
            assert msg['method']=='GET' and msg['path']=='history'
            await ws.send_json({'id':msg['id'],'status':200,'body':{'threads':{}}})
            assert await (await task).json()=={'threads':{}}
        for _ in range(10):
            if not any(d['device_id']=='speaker' for d in await devices()): break
        else: pytest.fail('disconnected speaker still reported online')
