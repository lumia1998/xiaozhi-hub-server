import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from core.agent import stickers, chat_history
from core.admin.admin_handler import AdminHandler, COOKIE_NAME


def picture(format='PNG'):
    output = io.BytesIO()
    image = Image.new('RGB', (24, 24), 'orange')
    if format == 'GIF':
        image.save(output, format=format, save_all=True, append_images=[Image.new('RGB', (24, 24), 'blue')], duration=100, loop=0)
    else:
        image.save(output, format=format)
    return base64.b64encode(output.getvalue()).decode()


def payload():
    return {'id': 'a' * 32, 'version': 0, 'name': '收到', 'tags': ['收到', '开心'],
            'description': '一只猫竖起大拇指', 'usage': '轻松确认收到消息', 'enabled': True, 'image': picture()}


def test_library_roundtrip_retry_and_conflict(tmp_path):
    store = stickers.StickerStore(tmp_path / 'stickers.db')
    body = payload()
    assert store.list() == []
    store.save(body)
    store.save(body)
    assert len(store.list()) == 1
    row = store.list()[0]
    assert row['tags'] == body['tags'] and row['usage'] == body['usage']
    store.save({**row, 'enabled': False})
    assert store.list(enabled_only=True) == []
    with pytest.raises(ValueError, match='修改'):
        store.save(row)
    with pytest.raises(ValueError, match='修改'):
        store.delete(row['id'], row['version'])
    store.delete(row['id'], 2)
    store.delete(row['id'], 2)
    assert store.list() == []


def test_old_library_migration(tmp_path):
    import sqlite3
    path = tmp_path / 'stickers.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE stickers(id TEXT PRIMARY KEY,name TEXT,description TEXT,enabled INTEGER,version INTEGER,mime TEXT,image BLOB)')
    store = stickers.StickerStore(path)
    store.save(payload())
    assert store.list()[0]['tags'] == ['收到', '开心']


@pytest.mark.parametrize('format', ['PNG', 'JPEG', 'WEBP', 'GIF'])
def test_formats_and_animation(format):
    encoded = picture(format)
    data, mime = stickers.validate_image(encoded)
    assert data == base64.b64decode(encoded)
    assert mime == stickers.MIME_TYPES[format]
    if format == 'GIF':
        assert Image.open(io.BytesIO(data)).n_frames == 2


@pytest.mark.parametrize('encoded', ['invalid', base64.b64encode(b'<svg></svg>').decode(), '', 'a' * (8 * 1024 * 1024)])
def test_invalid_images(encoded):
    with pytest.raises(ValueError):
        stickers.validate_image(encoded)


@pytest.mark.asyncio
async def test_admin_auth_upload_tagging_and_media(tmp_path, monkeypatch):
    handler = AdminHandler({}, config_path=tmp_path / 'config.yaml')
    app = web.Application()
    handler.register(app)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get('/admin/api/stickers')).status == 401
        assert (await client.post('/admin/api/stickers/save', json=payload())).status == 401
        client.session.cookie_jar.update_cookies({COOKIE_NAME: handler.sessions.create()})
        assert (await client.post('/admin/api/stickers/save', data='bad')).status == 415
        response = await client.post('/admin/api/stickers/save', json=payload())
        assert response.status == 200, await response.text()
        row = (await response.json())['items'][0]
        response = await client.get('/media/stickers/' + row['id'])
        assert response.content_type == 'image/png'
        assert await response.read() == base64.b64decode(payload()['image'])
        monkeypatch.setattr(handler, '_merged', lambda: {})
        monkeypatch.setattr(stickers, 'annotate', lambda config, encoded: stickers.metadata(payload()))
        response = await client.post('/admin/api/stickers/annotate', json={'id': row['id']})
        assert (await response.json())['tags'] == ['收到', '开心']
        assert (await client.post('/admin/api/stickers/delete', json={'id': row['id'], 'version': row['version']})).status == 200
        assert (await client.get('/media/stickers/' + row['id'])).status == 404


def test_vision_annotation_uses_small_jpeg_and_validates(monkeypatch):
    from core.utils import vllm
    def response(prompt, encoded):
        assert 'tags' in prompt
        assert Image.open(io.BytesIO(base64.b64decode(encoded))).format == 'JPEG'
        return '```json\n' + json.dumps(stickers.metadata(payload()), ensure_ascii=False) + '\n```'
    monkeypatch.setattr(vllm, 'create_instance', lambda *args: SimpleNamespace(response=response))
    assert stickers.annotate({'selected_module': {'VLLM': 'vision'}, 'VLLM': {'vision': {'type': 'openai'}}}, picture('GIF'))['tags'] == ['收到', '开心']
    with pytest.raises(ValueError, match='配置视觉模型'):
        stickers.annotate({}, picture())


@pytest.mark.asyncio
async def test_send_tool_uses_tags_and_persists_history(tmp_path, monkeypatch):
    store = stickers.StickerStore(tmp_path / 'stickers.db')
    store.save(payload())
    monkeypatch.setattr(stickers, 'default_path', lambda: store.path)
    conn = SimpleNamespace(features={'media': {'version': 1, 'kinds': ['image']}},
                           config={'server': {'ip': '10.1.2.30', 'http_port': 16688}}, session_id='session',
                           loop=asyncio.get_running_loop(), websocket=SimpleNamespace(send=AsyncMock()))
    assert {item['function']['name'] for item in stickers.definitions(conn)} == {'search_stickers', 'send_sticker'}
    candidates = json.loads(stickers.search({'query': '开心 收到'})['text'])['candidates']
    assert candidates[0]['tags'] == ['收到', '开心'] and candidates[0]['usage'] == '轻松确认收到消息'
    assert json.loads(stickers.search({'query': '不存在的标签'})['text'])['candidates'] == []
    result = await asyncio.to_thread(stickers.execute, conn, {'sticker_id': 'a' * 32}, 'call', 'turn', lambda: False)
    assert not result['error']
    media = json.loads(result['text'])['media']
    assert media[0]['url'] == 'http://10.1.2.30:16688/media/stickers/' + 'a' * 32
    db = chat_history.ChatDB(tmp_path / 'chat.db')
    monkeypatch.setattr(chat_history, 'db', lambda: db)
    try:
        db.insert('device', 'model', 'assistant', None, tool_calls=[{'id': 'call', 'type': 'function', 'function': {'name': 'send_sticker', 'arguments': '{}'}}])
        db.insert('device', 'model', 'tool', result['text'], tool_call_id='call')
        assert chat_history.history('device', 50)['model'][0]['media'] == media
    finally:
        db.close()
    store.save({**store.list()[0], 'enabled': False})
    assert stickers.definitions(conn) == []
    assert stickers.execute(conn, {'sticker_id': 'a' * 32}, 'call', 'turn', lambda: False)['error']


@pytest.mark.asyncio
async def test_pi_search_send_and_one_per_turn(tmp_path, monkeypatch):
    import queue
    import threading
    from unittest.mock import MagicMock
    from core.agent import pi_chat
    from core.utils.dialogue import Dialogue

    path = tmp_path / 'stickers.db'
    monkeypatch.setattr(stickers, 'default_path', lambda: path)
    stickers.StickerStore().save(payload())

    class Provider:
        model_name = 'test'

        def events(self, session, dialogue, functions, tool, stale):
            assert {'send_sticker', 'search_stickers'} <= {item['function']['name'] for item in functions}
            candidates = tool({'name': 'search_stickers', 'arguments': {'query': '收到'}, 'call_id': 'search'})
            sid = json.loads(candidates['text'])['candidates'][0]['id']
            sent = tool({'name': 'send_sticker', 'arguments': {'sticker_id': sid}, 'call_id': 'send'})
            assert not sent['error']
            repeated = tool({'name': 'send_sticker', 'arguments': {'sticker_id': sid}, 'call_id': 'again'})
            assert '本轮已经发送' in repeated['text']
            yield {'type': 'text', 'text': '收到啦'}

    conn = SimpleNamespace(
        features={'media': {'version': 1, 'kinds': ['image']}}, config={'server': {'ip': '10.1.2.30', 'http_port': 16688}},
        session_id='session', loop=asyncio.get_running_loop(), websocket=SimpleNamespace(send=AsyncMock()),
        llm=Provider(), dialogue=Dialogue(), logger=MagicMock(), stop_event=threading.Event(),
        intent_type='nointent', tts=SimpleNamespace(tts_text_queue=queue.Queue(), store_tts_text=MagicMock()),
        _superseded=lambda _: False, _put_reply=MagicMock(),
    )
    monkeypatch.setattr(pi_chat.chat_history, 'device_of', lambda _: '')
    monkeypatch.setattr(pi_chat.chat_history, 'memory_context_for', lambda _: {})
    monkeypatch.setattr(pi_chat.llm_router, 'not_ready_reply', lambda _: None)
    monkeypatch.setattr(pi_chat.extensions, 'enabled_skills', lambda _: {})
    await asyncio.to_thread(pi_chat.chat, conn, '发一张收到的表情', 'turn')
    conn.websocket.send.assert_awaited_once()
    spoken = [item.content_detail for item in conn.tts.tts_text_queue.queue if item.content_detail]
    assert spoken == ['收到啦']


@pytest.mark.asyncio
async def test_upload_over_default_aiohttp_limit(tmp_path):
    handler = AdminHandler({}, config_path=tmp_path / 'config.yaml')
    app = web.Application()
    handler.register(app)
    async with TestClient(TestServer(app)) as client:
        client.session.cookie_jar.update_cookies({COOKIE_NAME: handler.sessions.create()})
        body = payload()
        body['image'] = base64.b64encode(base64.b64decode(body['image']) + bytes(1200000)).decode()
        response = await client.post('/admin/api/stickers/save', json=body)
        assert response.status == 200, await response.text()
        body['image'] = 'A' * (9 * 1024 * 1024)
        response = await client.post('/admin/api/stickers/save', json=body)
        assert response.status == 413
