import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.agent import choice_cards, ha_commands
from core.utils import turn_gen
from plugins_func.pre_llm import home_assistant as home
from test.latency_report import summarize


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(choice_cards, 'identity', lambda conn: ('dev', 'owner', conn.epoch, 'model'))
    return SimpleNamespace(device_id='dev', session_id='session', sentence_id='turn', epoch=1, features={}, turn_gen=1,
        websocket=SimpleNamespace(send=AsyncMock()), config={'selected_module': {'Intent':'function_call'},
        'Intent': {'function_call': {'functions':['hass_state']}}, 'plugins': {'home_assistant':{
        'direct_commands': {'enabled':True}, 'devices':['卧室,台灯,light.bed','客厅,台灯,light.living']}}})


@pytest.mark.parametrize('text,route', [
    ('打开卧室台灯','direct'), ('把客厅台灯调暗一点','direct'), ('打开台灯','clarify'),
    ('请不要打开台灯','deny'), ('别关卧室台灯','deny'), ('如果天黑了就打开台灯','model'),
    ('刚才为什么关灯','model'), ('先关客厅台灯然后打开卧室台灯','model'), ('再暗一点','target')])
def test_route_corpus(conn, text, route):
    result = home.match(conn, text)
    actual = ('clarify' if isinstance(result, list) else 'direct' if isinstance(result, ha_commands.Command)
              else 'deny' if result == 'do_not_act' else 'target' if result == 'need_target' else 'model')
    assert actual == route


@pytest.mark.parametrize('text,expected', [
    ('呃打开筒灯', 'turn_on'), ('呃，打开筒灯', 'turn_on'), ('小葵，嗯，帮我开一下主卧的筒灯', 'turn_on'),
    ('开一下筒灯', 'turn_on'), ('关闭一下筒灯', 'turn_off'), ('把筒灯关了', 'turn_off'),
    ('请把主卧筒灯关掉了吧', 'turn_off'), ('呃，别把筒灯关了', 'do_not_act'), ('请不要开一下筒灯', 'do_not_act'),
    ('筒灯关了', None), ('把筒灯关了？', None), ('筒灯关了吗', None), ('他说把筒灯关了', None),
    ('如果天黑就开一下筒灯', None), ('把筒灯关了再开主灯', None), ('打开阳台灯、屏幕挂灯、主灯、筒灯', None),
    ('打开筒灯防闪烁模式', None), ('开一下桶灯', None)])
def test_downlight_speech(conn, text, expected):
    conn.config['plugins']['home_assistant']['devices'] = ['主卧,筒灯,switch.downlight', '主卧,主灯,switch.main']
    result = home.match(conn, text)
    if expected in ('turn_on', 'turn_off'):
        assert (result.action, result.device.entity_id) == (expected, 'switch.downlight')
    else:
        assert result == expected


def test_spoken_short_name_still_requires_room(conn):
    result = home.match(conn, '呃，开一下台灯')
    assert {command.device.entity_id for command in result} == {'light.bed', 'light.living'}


@pytest.mark.asyncio
async def test_clarify_then_voice_followup_and_status_only(conn, monkeypatch):
    execute = AsyncMock(return_value=ha_commands.ExecutionResult('已打开。', True))
    monkeypatch.setattr(ha_commands, 'execute', execute)
    result = await home.intercept(conn, '打开台灯')
    assert '卧室台灯' in result['reply'] and '客厅台灯' in result['reply']
    execute.assert_not_awaited()
    turn_gen.bump(conn)
    assert (await home.intercept(conn, '卧室那个'))['status'] == 'confirmed'
    assert execute.await_args.args[0].device.entity_id == 'light.bed'
    execute.return_value = ha_commands.ExecutionResult('状态尚未更新。', status='submitted')
    turn_gen.bump(conn)
    assert (await home.intercept(conn,'再暗一点'))['status'] == 'submitted'
    assert execute.await_args.args[0].action == 'brightness_down'
    turn_gen.bump(conn)
    await home.intercept(conn,'刚才成功了吗')
    assert [call.args[0].action for call in execute.await_args_list] == ['turn_on','brightness_down','state']


@pytest.mark.parametrize('invalidate', ['timeout','topic','abort','owner'])
@pytest.mark.asyncio
async def test_expired_target_never_operates(conn, monkeypatch, invalidate):
    execute = AsyncMock(return_value=ha_commands.ExecutionResult('已打开。', True))
    monkeypatch.setattr(ha_commands,'execute',execute)
    await home.intercept(conn,'打开卧室台灯')
    if invalidate == 'timeout': conn.ha_context['until'] = 0
    if invalidate == 'topic': turn_gen.bump(conn)
    if invalidate == 'abort': conn.ha_context = None
    if invalidate == 'owner': conn.epoch += 1
    turn_gen.bump(conn)
    assert '房间' in (await home.intercept(conn,'再暗一点'))['reply']
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_card_and_voice_share_single_selection(conn, monkeypatch):
    conn.features = {'choices':{'version':1}}
    show = Mock(return_value={'error':False,'text':json.dumps({'choice':{'id':'ha_1'}})})
    monkeypatch.setattr(choice_cards,'execute',show)
    select = Mock(return_value={'duplicate':False})
    monkeypatch.setattr(choice_cards,'select',select)
    execute = AsyncMock(return_value=ha_commands.ExecutionResult('已打开。',True))
    monkeypatch.setattr(ha_commands,'execute',execute)
    await home.intercept(conn,'打开台灯')
    assert show.call_args.kwargs['device_choice']
    turn_gen.bump(conn)
    await home.intercept(conn,'第1个')
    select.assert_called_once_with(conn, 'ha_1','1')
    execute.assert_awaited_once()
    assert json.loads(conn.websocket.send.await_args.args[0])['id'] == 'ha_1'
    turn_gen.bump(conn)
    assert home.match(conn,'第1个') is None


def test_latency_report_joins_only_anonymous_stages():
    trace = 'a'*32
    lines = [f'turn_latency id={trace} stage={stage} ms={ms}' for stage, ms in
             [('speech_end_to_output',1220),('route_model',1),('mcp_connect_cold',80),('api_key',123)]]
    rows = list(summarize(lines))
    assert len(rows) == 3
    assert ('model/mcp-cold','speech_end_to_output',1,1220,1220) in rows


@pytest.mark.asyncio
async def test_button_through_listen_handler_does_not_repeat(conn, monkeypatch):
    import queue
    import threading
    from core.handle import receiveAudioHandle as receive, sendAudioHandle
    from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
    from core.utils.dialogue import Dialogue
    from core.agent import chat_history

    conn.features = {'choices': {'version': 1}}
    conn.need_bind = conn.client_is_speaking = False
    conn.max_output_size = 0
    conn.client_listen_mode = 'realtime'
    conn.stop_event = threading.Event()
    conn.dialogue = Dialogue()
    conn.logger = Mock()
    conn.tts = Mock(tts_text_queue=queue.Queue())
    monkeypatch.setattr(receive, 'handle_call_name', AsyncMock(side_effect=lambda _, text: text))
    monkeypatch.setattr(receive.clear_command, 'handle', AsyncMock(side_effect=lambda _, text: text))
    monkeypatch.setattr(sendAudioHandle, 'send_stt_message', AsyncMock())
    monkeypatch.setattr(chat_history, 'sync', Mock())
    monkeypatch.setattr(chat_history, 'append_operation_event_for', Mock())
    monkeypatch.setattr(choice_cards, 'execute', Mock(return_value={'error':False, 'text':json.dumps({'choice':{'id':'ha_1'}})}))
    monkeypatch.setattr(choice_cards, 'select', Mock(return_value={'duplicate':False}))
    execute = AsyncMock(return_value=ha_commands.ExecutionResult('已打开。',True))
    monkeypatch.setattr(ha_commands, 'execute', execute)
    await receive.startToChat(conn, '打开台灯')
    request = {'state':'detect', 'choice':{'id':'ha_1','option_id':'2'}}
    handler = ListenTextMessageHandler()
    await handler.handle(conn,request)
    assert json.loads(conn.websocket.send.await_args.args[0])['ok'] is True
    await handler.handle(conn,request)
    assert json.loads(conn.websocket.send.await_args.args[0])['ok'] is False
    execute.assert_awaited_once()
