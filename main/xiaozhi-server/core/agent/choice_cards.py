"""Persistent, owner-bound choices. Button submissions use the existing listen channel."""
import asyncio
import json
import sqlite3
import time
import uuid
import re
from contextlib import contextmanager

from core.admin import config_store
from core.agent import chat_history, media_output

TOOL_NAME = 'ask_choice'
DEFINITION = {'type': 'function', 'function': {
    'name': TOOL_NAME,
    'description': '需要用户在2至4个方案中选择一个时，在音箱屏幕展示选项卡片。每轮一张。'
                   '调用后简短说明问题并等待用户选择，不替用户选择，不把卡片JSON念出来。用户也可以用语音回答。',
    'parameters': {'type': 'object', 'properties': {
        'question': {'type': 'string', 'maxLength': 200},
        'options': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': {
            'type': 'object', 'properties': {'label': {'type': 'string', 'maxLength': 60},
                                          'description': {'type': 'string', 'maxLength': 160}},
            'required': ['label'], 'additionalProperties': False}}},
        'required': ['question', 'options'], 'additionalProperties': False}}}


def supported(conn):
    return (getattr(conn, 'features', {}) or {}).get('choices', {}).get('version') == 1


def identity(conn):
    device = chat_history.device_of(conn)
    owner = chat_history.owner_for_device(device)
    epoch = chat_history.db().owner_epoch(device)
    model = (getattr(conn, 'hub_route', {}) or {}).get('current') or (conn.config.get('selected_module') or {}).get('LLM', '')
    return device, owner, epoch, model


@contextmanager
def database():
    path = config_store.custom_config_path().parent / 'choices.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS choices (
            id TEXT PRIMARY KEY, device TEXT, owner TEXT, epoch INTEGER, model TEXT,
            card TEXT, expires REAL, selected TEXT)''')
        with db:
            yield db
    finally:
        db.close()


def validate(arguments):
    if not isinstance(arguments, dict):
        raise ValueError('选项格式无效')
    question, options = arguments.get('question'), arguments.get('options')
    if not isinstance(question, str) or not question.strip() or len(question) > 200:
        raise ValueError('问题需要1至200个字符')
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        raise ValueError('需要2至4个选项')
    normalized = []
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            raise ValueError('选项格式无效')
        label, description = option.get('label'), option.get('description', '')
        if not isinstance(label, str) or not label.strip() or len(label) > 60:
            raise ValueError('选项名称需要1至60个字符')
        if not isinstance(description, str) or len(description) > 160:
            raise ValueError('选项说明最多160个字符')
        normalized.append({'id': str(index + 1), 'label': label.strip(), 'description': description.strip()})
    if len({option['label'] for option in normalized}) != len(normalized):
        raise ValueError('选项名称不能重复')
    return {'question': question.strip(), 'options': normalized}


def execute(conn, arguments, call_id, sentence_id, stale, *, device_choice=False):
    try:
        if not supported(conn) or stale():
            raise ValueError('当前设备无法展示选项或本轮已取消')
        card = validate(arguments)
        device, owner, epoch, model = identity(conn)
        if not device:
            raise ValueError('设备身份无效')
        card.update(id=('ha_' if device_choice else '') + uuid.uuid4().hex, selected=None,
                    expires=time.time() + (45 if device_choice else 86400))
        with database() as db:
            db.execute('DELETE FROM choices WHERE expires < ?', (time.time() - 86400 * 7,))
            db.execute('INSERT INTO choices VALUES(?,?,?,?,?,?,?,NULL)',
                       (card['id'], device, owner, epoch, model, json.dumps(card, ensure_ascii=False), card['expires']))
        payload = {'type': 'choice_card', 'version': 1, 'session_id': conn.session_id,
                   'sentence_id': sentence_id, 'card': card}
        future = asyncio.run_coroutine_threadsafe(media_output._send(conn, payload, stale), conn.loop)
        try:
            future.result(timeout=5)
        except Exception:
            future.cancel()
            with database() as db:
                db.execute('DELETE FROM choices WHERE id=?', (card['id'],))
            raise
        conn.last_choice_id = card['id']
        return {'text': json.dumps({'status': 'shown', 'choice': card}, ensure_ascii=False), 'error': False, 'terminate': False}
    except Exception:
        return {'text': '选项卡片发送失败，请用文字说明选项。', 'error': True, 'terminate': False}


def select(conn, card_id, option_id):
    device, owner, epoch, model = identity(conn)
    if not isinstance(card_id, str) or not isinstance(option_id, str):
        raise ValueError('选择参数无效')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM choices WHERE id=? AND device=? AND owner=? AND epoch=? AND model=?',
                         (card_id, device, owner, epoch, model)).fetchone()
        if not row or row['expires'] < time.time():
            raise ValueError('选项已过期或属于其他对话，请重新提问')
        card = json.loads(row['card'])
        option = next((item for item in card['options'] if item['id'] == option_id), None)
        if not option:
            raise ValueError('选项不存在')
        if row['selected']:
            if row['selected'] != option_id:
                raise ValueError('该问题已经选择了其他选项')
            return {'duplicate': True, 'text': '', 'selected': option_id}
        db.execute('UPDATE choices SET selected=? WHERE id=?', (option_id, card_id))
    return {'duplicate': False, 'selected': option_id,
            'text': f'关于“{card["question"]}”，我选择第{option_id}项“{option["label"]}”。'}


def history_card(content, device, model):
    try:
        card = json.loads(content).get('choice')
        if not isinstance(card, dict):
            return None
        with database() as db:
            row = db.execute('SELECT * FROM choices WHERE id=? AND device=? AND model=? AND owner=? AND epoch=?',
                             (card['id'], device, model, chat_history.owner_for_device(device), chat_history.db().owner_epoch(device))).fetchone()
        card['selected'] = row['selected'] if row else None
        card['expired'] = not row or row['expires'] < time.time()
        return card
    except (ValueError, TypeError, KeyError):
        return None


def voice_selection(conn, text):
    card_id = getattr(conn, 'last_choice_id', None)
    if not card_id or not isinstance(text, str):
        return None
    match = re.fullmatch(r'(?:我想|我要|我)?(?:选|选择)?第?([一二三四1234])(?:个|项|种|个方案|个选项)?[。！!？?\s]*', text.strip())
    if not match:
        return None
    value = match.group(1)
    option_id = str('一二三四'.index(value) + 1) if value in '一二三四' else value
    try:
        result = select(conn, card_id, option_id)
    except ValueError:
        return None
    return None if result['duplicate'] else {**result, 'id': card_id}
