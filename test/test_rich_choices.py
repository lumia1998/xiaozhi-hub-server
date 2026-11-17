import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent import choice_cards, image_reader, rich_output


@pytest.mark.parametrize('size', [1, 2, 3, 7, 64, 1000])
def test_speech_stream_separates_visual_content(size):
    text = '这里有示例。\n```python\nprint("秘密正文")\n```\n看图：![示例](https://example.com/a(b).png)。详见[说明](https://example.com/doc)。结束。'
    speech = rich_output.SpeechFilter()
    result = ''.join(speech.feed(text[i:i + size]) for i in range(0, len(text), size)) + speech.flush()
    assert '秘密正文' not in result and 'https' not in result and 'example' not in result
    assert '代码已显示在屏幕上' in result and '图片请看屏幕' in result and '结束' in result


def test_unclosed_fence_and_indented_code():
    speech = rich_output.SpeechFilter()
    assert 'secret' not in speech.feed('```python\nsecret') + speech.flush()
    assert '下一句话' in speech.feed('下一句话。') + speech.flush()
    assert 'secret' not in speech.feed('\n    secret\n结束。') + speech.flush()


@pytest.mark.asyncio
async def test_choice_scope_retry_history_and_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(choice_cards.config_store, 'custom_config_path', lambda: tmp_path / 'config.yaml')
    keys = ['speaker', 'owner', 1, 'model']
    monkeypatch.setattr(choice_cards, 'identity', lambda _: tuple(keys))
    monkeypatch.setattr(choice_cards.chat_history, 'owner_for_device', lambda _: keys[1])
    monkeypatch.setattr(choice_cards.chat_history, 'db', lambda: SimpleNamespace(owner_epoch=lambda _: keys[2]))
    conn = SimpleNamespace(features={'choices': {'version': 1}}, session_id='session', loop=asyncio.get_running_loop(),
                           websocket=SimpleNamespace(send=AsyncMock()))
    args = {'question': '去哪儿？', 'options': [{'label': '户外'}, {'label': '室内', 'description': '看展'}]}
    result = await asyncio.to_thread(choice_cards.execute, conn, args, 'call', 'turn', lambda: False)
    card = json.loads(result['text'])['choice']
    assert card['options'][0]['id'] == '1'
    for index, wrong in enumerate(('other device', 'other owner', 2, 'other model')):
        old, keys[index] = keys[index], wrong
        with pytest.raises(ValueError):
            choice_cards.select(conn, card['id'], '1')
        keys[index] = old
    with pytest.raises(ValueError):
        choice_cards.select(conn, card['id'], '3')
    first = choice_cards.voice_selection(conn, '我选第二个。')
    assert '室内' in first['text']
    assert choice_cards.select(conn, card['id'], '2')['duplicate']
    assert choice_cards.history_card(result['text'], 'speaker', 'model')['selected'] == '2'
    with pytest.raises(ValueError):
        choice_cards.select(conn, card['id'], '1')


@pytest.mark.parametrize('url', ['http://127.0.0.1/a', 'http://[::1]/a', 'http://192.168.1.10/image', 'file:///etc/a', 'http://a@b.com/x'])
def test_image_url_scope(url):
    with pytest.raises(ValueError):
        image_reader.validate_url(url)
