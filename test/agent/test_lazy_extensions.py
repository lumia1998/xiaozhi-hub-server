import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.agent import extensions, tool_status
from core.providers.tools.server_mcp import mcp_manager
from plugins_func.functions import hass_state
from plugins_func.pre_llm import clock, home_assistant


def test_skill_catalog_and_named_loading(tmp_path):
    path = tmp_path / 'extensions.json'
    row = dict(id='trip', enabled=True, engines=['api'], name='出行', description='出行建议', content='独有的完整流程')
    path.write_text(json.dumps({'skills': [row, {**row, 'id': 'private', 'enabled': False}]}))
    dialogue = [{'role': 'system', 'content': '原提示'}]
    catalog = extensions.add_skills(dialogue, 'api', path)[0]['content']
    assert 'trip' in catalog and '出行建议' in catalog and 'private' not in catalog and row['content'] not in catalog
    for query in ('使用出行技能，帮我做计划', '/trip 计划'):
        assert row['content'] in extensions.add_skills(dialogue, 'api', path, query=query)[0]['content']
    assert dialogue[0]['content'] == '原提示'


@pytest.mark.asyncio
async def test_direct_clock_crosses_midnight_and_rejects_complex_requests():
    conn = SimpleNamespace(config={'server': {'timezone_offset': 8}})
    instant = datetime(2026, 11, 17, 23, 15, tzinfo=timezone.utc)
    with patch.object(clock, 'datetime') as source:
        source.now.side_effect = instant.astimezone
        assert (await clock.intercept(conn, '请问，现在几点？', None))['reply'] == '现在是7点15分。'
        assert (await clock.intercept(conn, '今天几号', None))['reply'] == '今天是11月18日。'
        conn.features = {'timezone': 'UTC'}
        assert (await clock.intercept(conn, '今天星期几', None))['reply'] == '今天是星期二。'
    for text in ('纽约现在几点', '现在几点，顺便关灯', '明天几点下雨'):
        assert clock.match(conn, text) is None


@pytest.mark.asyncio
async def test_ha_read_only_and_unlisted_target_never_execute():
    conn = SimpleNamespace(config={'selected_module': {'Intent': 'function_call'},
        'Intent': {'function_call': {'functions': ['hass_get_state']}}, 'plugins': {'home_assistant': {
            'devices': ['卧室,台灯,light.lamp'], 'direct_commands': {'enabled': True}}}})
    with patch.object(home_assistant.ha_commands, 'execute', AsyncMock()) as direct, patch.object(hass_state, 'initialize_hass_handler') as model:
        assert '未授权' in (await home_assistant.intercept(conn, '打开台灯'))['reply']
        assert '未授权' in await hass_state.handle_hass_set_state(conn, 'light.lamp', {'type': 'turn_on'})
        assert '未授权' in await hass_state.handle_hass_get_state(conn, 'light.other')
        direct.assert_not_awaited()
        model.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_cache_is_lazy_scoped_and_revalidated(monkeypatch):
    monkeypatch.setattr(mcp_manager, '_catalogs', {})
    config = {'allowed_tools': ['read'], 'timeout': 1}
    monkeypatch.setattr(mcp_manager.ServerMCPManager, 'load_config', lambda _: {'demo': config.copy()})
    definition = {'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}
    def client(cfg):
        return SimpleNamespace(config=cfg, name_mapping={'read': 'read', 'write': 'write'},
            get_available_tools=lambda: [json.loads(json.dumps(definition)), {'function': {'name': 'write'}}],
            initialize=AsyncMock(), cleanup=AsyncMock(), call_tool=AsyncMock(return_value='ok'))
    factory = Mock(side_effect=client)
    monkeypatch.setattr(mcp_manager, 'ServerMCPClient', factory)
    def manager(): return mcp_manager.ServerMCPManager(SimpleNamespace())
    cold, warm, stale = manager(), manager(), manager()
    await cold.initialize_servers()
    await warm.initialize_servers()
    await stale.initialize_servers()
    assert factory.call_count == 1 and len(warm.routes) == 1
    name, = warm.routes
    assert await asyncio.gather(warm.execute_tool(name, {}), warm.execute_tool(name, {})) == ['ok', 'ok']
    assert factory.call_count == 2
    warm.clients['demo'].call_tool.side_effect = TimeoutError
    with pytest.raises(RuntimeError, match='未自动重试'):
        await warm.execute_tool(name, {})
    assert warm.clients['demo'].call_tool.await_count == 3
    definition['function']['description'] = 'changed'
    with pytest.raises(ValueError, match='变更'):
        await stale.execute_tool(name, {})
    stale.clients['demo'].call_tool.assert_not_called()
    expired = manager()
    monkeypatch.setattr(mcp_manager, 'time', SimpleNamespace(monotonic=lambda: float('inf')))
    await expired.initialize_servers()
    assert factory.call_count == 4 and expired.routes
    config['allowed_tools'] = []
    denied = manager()
    await denied.initialize_servers()
    assert not denied.routes and factory.call_count == 4
    for instance in (cold, warm, stale, expired, denied): await instance.cleanup_all()


@pytest.mark.asyncio
async def test_status_only_for_slow_tools():
    conn = SimpleNamespace(features={'tool_status': 1}, session_id='s', websocket=SimpleNamespace(send=AsyncMock()))
    async with tool_status.observing(conn, 'get_weather'):
        pass
    conn.websocket.send.assert_not_awaited()
    started = asyncio.Event()
    conn.websocket.send.side_effect = lambda _: started.set()
    async with tool_status.observing(conn, 'web_search'):
        await asyncio.wait_for(started.wait(), timeout=2)
    start, end = [json.loads(call.args[0]) for call in conn.websocket.send.await_args_list]
    assert start['state'] == 'start' and end == {**start, 'state': 'end'} and start['category'] == 'search'
