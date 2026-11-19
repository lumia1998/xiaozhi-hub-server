"""Device command hook: match -> HA REST -> {handled, reply}; never invokes a model."""
import asyncio
import json
import time
from core.agent import choice_cards, ha_commands
from core.utils import turn_gen

NAME = 'home_assistant'


def active(conn, *, previous=False):
    context = getattr(conn, 'ha_context', None)
    if context and context['until'] > time.monotonic() and context['gen'] == turn_gen.current(conn) - int(previous):
        if context['identity'] == choice_cards.identity(conn):
            return context
    return None


def remember(conn, commands, card=None, confirmed=False):
    if getattr(conn, 'device_id', None):
        conn.ha_context = dict(commands=commands, card=card, confirmed=confirmed, identity=choice_cards.identity(conn),
                               gen=turn_gen.current(conn), until=time.monotonic()+45)


def selection(context, text):
    answer = ha_commands.normalize(text).removesuffix('那个')
    matches = [(index, command) for index, command in enumerate(context['commands'])
               if answer and answer in {command.device.room, command.device.label, '第'+'一二三四'[index]+'个', '第'+str(index+1)+'个'}]
    return matches[0] if len(matches) == 1 else (None, None)


def match(conn, text):
    config = (conn.config.get('plugins') or {}).get('home_assistant') or {}
    if (config.get('direct_commands') or {}).get('enabled') is not True:
        return None
    matches = ha_commands.candidates(text, config)
    if matches:
        return matches[0] if len(matches) == 1 else matches
    phrase = ha_commands.command_body(text)
    for prefix in ('不要', '别'):
        if phrase.startswith(prefix) and ha_commands.candidates(phrase[len(prefix):], config):
            return 'do_not_act'
    context = active(conn, previous=True)
    if context and len(context['commands']) > 1:
        return selection(context, text)[1]
    if phrase in ('再暗一点', '再亮一点', '现在什么状态', '刚才成功了吗'):
        if context and len(context['commands']) == 1:
            device = context['commands'][0].device
            if phrase in ('现在什么状态', '刚才成功了吗'):
                return ha_commands.Command(device, 'state')
            if context['confirmed'] and device.domain == 'light':
                return ha_commands.Command(device, 'brightness_down' if phrase == '再暗一点' else 'brightness_up')
        return 'need_target'
    return None


async def intercept(conn, text, stale=lambda: False):
    command = match(conn, text)
    if command is None:
        return {'handled': False}
    context = active(conn, previous=True)
    conn.ha_context = None
    if command == 'do_not_act':
        return {'handled': True, 'reply': '没有执行设备操作。', 'record_operation': False}
    if command == 'need_target':
        return {'handled': True, 'reply': '请说一下设备名称和房间。', 'record_operation': False}
    if isinstance(command, list):
        if not all(ha_commands.allowed(conn.config, item.device.entity_id, write=item.action != 'state') for item in command):
            return {'handled': True, 'reply': '当前设置未授权这项设备操作。', 'record_operation': False}
        labels = [item.device.label for item in command]
        if len(labels) > 4 or len(set(labels)) != len(labels):
            return {'handled': True, 'reply': '请说出完整房间和设备名称。', 'record_operation': False}
        question = '你指的是' + '还是'.join(labels) + '？'
        card = None
        if choice_cards.supported(conn):
            result = await asyncio.to_thread(choice_cards.execute, conn, {'question': question,
                'options': [{'label': label} for label in labels]}, None, conn.sentence_id, stale, device_choice=True)
            if not result['error']:
                card = json.loads(result['text'])['choice']['id']
        if not stale():
            remember(conn, command, card=card)
        return {'handled': True, 'reply': question, 'record_operation': False}
    if not ha_commands.allowed(conn.config, command.device.entity_id, write=command.action != 'state'):
        return {'handled': True, 'reply': '当前设置未授权这项设备操作。', 'record_operation': False}
    config = conn.config['plugins']['home_assistant']
    if context and context['card'] and len(context['commands']) > 1:
        index, selected = selection(context, text)
        if selected == command:
            try:
                result = await asyncio.to_thread(choice_cards.select, conn, context['card'], str(index+1))
            except ValueError:
                return {'handled': True, 'reply': '选项已失效，请重新说出设备名称。', 'record_operation': False}
            if result['duplicate']:
                return {'handled': True, 'reply': None}
            await conn.websocket.send(json.dumps({'type':'choice_result','session_id':conn.session_id,'ok':True,
                                                 'id':context['card'],'selected':str(index+1)}))
    try:
        if stale():
            return {'handled': True, 'reply': None, 'cancelled': True}
        outcome = await ha_commands.execute(command, config, stale)
    except ha_commands.Superseded:
        return {'handled': True, 'reply': None, 'cancelled': True}
    reply = str(outcome)
    if not stale():
        remember(conn, [command], confirmed=getattr(outcome, 'confirmed', False))
    # A failed/uncertain HTTP request is still handled: never fall back and repeat it via LLM.
    return {'handled': True, 'reply': reply, 'action': command.action, 'entity': command.device.entity_id,
            'confirmed': bool(getattr(outcome, 'confirmed', False)), 'status': getattr(outcome, 'status', 'unknown')}
