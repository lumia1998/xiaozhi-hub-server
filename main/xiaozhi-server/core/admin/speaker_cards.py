"""Display layout only; visibility never grants HA read/write tool permissions."""
import asyncio
import json
import sqlite3
from contextlib import closing

import aiohttp

from core.agent import ha_commands
from core.api.panel_controls import describe


def default_ids(cards):
    rooms = {card['room'] for card in cards if card['controls'] and card['room'] != '未分房间'}
    return [card['id'] for card in cards if card['controls'] or card['room'] in rooms]


def layout(path, selection=None, expected=None):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS layout (key INTEGER PRIMARY KEY, rev INTEGER, value TEXT)')
        db.execute('BEGIN IMMEDIATE' if selection is not None else 'BEGIN')
        row=db.execute('SELECT rev,value FROM layout WHERE key=1').fetchone()
        rev, selected=(row[0],json.loads(row[1])) if row else (0,None)
        if selection is not None:
            if expected != rev: raise ValueError('布局已变更，请刷新后再保存')
            if not isinstance(selection,list) or len(selection)>500 or any(not isinstance(key,str) or not 1<=len(key)<=128 for key in selection):
                raise ValueError('卡片选择无效')
            selected=list(dict.fromkeys(selection)); rev+=1
            db.execute('INSERT OR REPLACE INTO layout VALUES(1,?,?)',(rev,json.dumps(selected)))
        return rev,selected


async def catalog(config):
    plugins=config.get('plugins') or {}; ha=plugins.get('home_assistant') or plugins.get('hass_state') or {}
    base=(ha.get('base_url') or '').rstrip('/'); token=ha.get('api_key')
    if not base or not token: raise ValueError('尚未配置 Home Assistant')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
        async with client.get(base+'/api/states',headers={'Authorization':'Bearer '+token},allow_redirects=False) as response:
            response.raise_for_status(); states={row['entity_id']:row for row in await response.json()}
        async with client.ws_connect(base+'/api/websocket',max_msg_size=16*1024*1024) as ws:
            if (await ws.receive_json())['type']!='auth_required': raise ValueError('无法读取 HA 注册表')
            await ws.send_json({'type':'auth','access_token':token})
            if (await ws.receive_json())['type']!='auth_ok': raise ValueError('HA 注册表认证失败')
            registry={}
            for index,kind in enumerate(('area','device','entity'),1):
                await ws.send_json({'id':index,'type':f'config/{kind}_registry/list'})
                result=await ws.receive_json()
                if not result.get('success') or result.get('id')!=index: raise ValueError('HA 注册表读取失败')
                registry[kind]=result['result']
    return build(config,ha,registry,states)


def build(config,ha,registry,states):
    areas={row['area_id']:row['name'] for row in registry['area']}
    members={}
    for row in registry['entity']:
        if not row.get('disabled_by'): members.setdefault(row.get('device_id'),[]).append(row['entity_id'])
    allowed={item.entity_id:item for item in ha_commands.devices(ha) if ha_commands.allowed(config,item.entity_id,write=False)}
    cards=[]
    for device in registry['device']:
        if device.get('disabled_by'): continue
        key=device['id']; room=areas.get(device.get('area_id'),'未分房间'); controls=[]; readings=[]; available=False
        for entity in members.get(key,[]):
            row=states.get(entity,{}); state=row.get('state'); attrs=row.get('attributes') or {}
            if state not in (None,'unknown','unavailable'):
                available=True
                if attrs.get('device_class') in ('temperature','humidity') and entity.startswith('sensor.'):
                    try: readings.append(f"{float(state):g}{attrs.get('unit_of_measurement','')}")
                    except (ValueError,TypeError): pass
            if entity in allowed:
                controls.append(describe(config, allowed[entity], row, room))
        cards.append(dict(id=key,room=room,name=device.get('name_by_user') or device.get('name') or '未命名设备',
                          controls=controls,status=' / '.join(dict.fromkeys(readings)) or ('有可用实体' if available else '暂无可用状态')))
    return sorted(cards,key=lambda item:(item['room'],not item['controls'],item['name']))
