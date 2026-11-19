import asyncio
import copy
import json
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from core.agent import ha_commands as ha
# The upstream logger initializes config with asyncio.run during its first import.
# Match production startup: load audio modules before starting async test loops.
from core.handle import receiveAudioHandle as receive

LIGHT = 'light.desk'
SWITCH = 'switch.main'
PLAYER = 'media_player.screen'
CONFIG = {'base_url': 'http://ha.test:8123', 'api_key': 'test-only',
          'devices': ['主卧,台灯,' + LIGHT, '主卧,主灯,' + SWITCH, '主卧,智慧屏音箱,' + PLAYER],
          'direct_commands': {'enabled': True, 'aliases': {PLAYER: ['智慧屏', '主卧智慧屏']}}}


def state(entity=LIGHT, value='on'):
    return {'entity_id': entity, 'state': value, 'attributes': {
        'brightness': 128, 'supported_color_modes': ['color_temp'],
        'volume_level': .4, 'supported_features': 16397, 'is_volume_muted': False}}


class ParserTests(unittest.TestCase):
    def test_common_commands(self):
        cases = [
            ('打开主卧主灯', SWITCH, 'turn_on', None), ('小葵，帮我把主灯关掉吧！', SWITCH, 'turn_off', None),
            ('主卧的台灯打开', LIGHT, 'turn_on', None), ('关闭台灯', LIGHT, 'turn_off', None),
            ('小葵同学，麻烦你帮我把台灯亮度调到百分之五十', LIGHT, 'brightness', 50),
            ('将主卧台灯设为30%', LIGHT, 'brightness', 30), ('台灯亮度调至百分之零', LIGHT, 'brightness', 0),
            ('把台灯调暗一点', LIGHT, 'brightness_down', None), ('调亮台灯', LIGHT, 'brightness_up', None),
            ('台灯亮一点', LIGHT, 'brightness_up', None), ('主灯开着吗？', SWITCH, 'state', None),
            ('查询主卧台灯的状态', LIGHT, 'state', None), ('看看主灯', SWITCH, 'state', None),
            ('台灯亮度是多少', LIGHT, 'state', None), ('智慧屏音量多少', PLAYER, 'state', None),
            ('智慧屏音量调到百分之二十', PLAYER, 'volume', 20), ('智慧屏的音量调小一点', PLAYER, 'volume_down', None),
            ('智慧屏声音大一些', PLAYER, 'volume_up', None), ('暂停智慧屏播放', PLAYER, 'pause', None),
            ('智慧屏继续播放', PLAYER, 'play', None), ('智慧屏静音', PLAYER, 'mute', None),
            ('主灯开', SWITCH, 'turn_on', None), ('台灯关', LIGHT, 'turn_off', None),
            ('智慧屏停', PLAYER, 'stop', None), ('停下智慧屏', PLAYER, 'stop', None),
            ('智慧屏停止播放', PLAYER, 'stop', None), ('智慧屏开', PLAYER, 'turn_on', None),
            ('取消智慧屏的静音', PLAYER, 'unmute', None), ('台灯亮度调到５０％', LIGHT, 'brightness', 50),
        ]
        for text, entity, action, value in cases:
            with self.subTest(text=text):
                parsed = ha.parse(text, CONFIG)
                self.assertIsNotNone(parsed)
                self.assertEqual((parsed.device.entity_id, parsed.action, parsed.value), (entity, action, value))

    def test_uncertain_or_unsupported_language_falls_through(self):
        cases = ['不要打开台灯', '别关主灯', '小葵不要关闭台灯', '打开台灯吗', '如果天黑了就打开台灯',
                 '五分钟后打开台灯', '把刚才那个调暗一点', '打开台灯和主灯', '打开台灯然后关闭主灯',
                 '把灯打开', '把所有灯关闭', '打开客厅台灯', '打开主灯是什么意思', '他说打开主灯',
                 '“打开主灯”', '请解释怎么打开台灯', '关卧室灯', '智慧屏播放周杰伦',
                 '主灯亮度调到50%', '台灯音量调到50%', '智慧屏亮度调到50%', '台灯亮度调到101%',
                 '台灯亮度调到-1%', '台灯亮度调到10.5%', '台灯亮度调到百分之十一百',
                 '台灯亮度调到零十', '台灯亮度调到两百', '台灯亮度调到一二', '退下', '结束', '小葵',
                 '智慧屏不要停', '停是什么意思', '台灯停', '如果声音大就停智慧屏', '智慧屏停然后开主灯']
        for text in cases:
            with self.subTest(text=text): self.assertIsNone(ha.parse(text, CONFIG))

    def test_ambiguous_short_names_and_unlisted_aliases(self):
        config = copy.deepcopy(CONFIG)
        config['devices'].append('客厅,台灯,light.other')
        config['direct_commands']['aliases']['light.not_configured'] = ['吊灯']
        self.assertIsNone(ha.parse('打开台灯', config))
        self.assertIsNone(ha.parse('打开吊灯', config))
        self.assertEqual(ha.parse('打开主卧台灯', config).device.entity_id, LIGHT)
        self.assertEqual(ha.parse('打开客厅台灯', config).device.entity_id, 'light.other')

    def test_numbers(self):
        for text, expected in [('零', 0), ('两', 2), ('十', 10), ('十一', 11), ('二十', 20),
                               ('九十九', 99), ('一百', 100), ('百分之50', 50), ('100%', 100)]:
            self.assertEqual(ha.percent(text), expected)


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_payload_and_confirmed_state(self):
        cases = [('打开台灯', 'turn_on', {}, 'on'), ('关闭主灯', 'turn_off', {}, 'off'),
                 ('台灯亮度调到50%', 'turn_on', {'brightness_pct': 50}, 'on'),
                 ('台灯亮度调到0%', 'turn_off', {}, 'off'),
                 ('台灯调暗一点', 'turn_on', {'brightness_pct': 40}, 'on'),
                 ('智慧屏音量调到20%', 'volume_set', {'volume_level': .2}, 'on'),
                 ('智慧屏音量调大一点', 'volume_set', {'volume_level': .5}, 'on'),
                 ('智慧屏静音', 'volume_mute', {'is_volume_muted': True}, 'on'),
                 ('智慧屏暂停', 'media_pause', {}, 'paused'),
                 ('智慧屏继续播放', 'media_play', {}, 'playing'),
                 ('智慧屏停', 'media_stop', {}, 'idle')]
        for text, service, args, final_state in cases:
            with self.subTest(text=text):
                command = ha.parse(text, CONFIG); current = state(command.device.entity_id)
                current['attributes']['supported_features'] |= 16
                posts = []
                async def handler(request):
                    self.assertEqual(request.headers['Authorization'], 'Bearer test-only')
                    if request.method == 'POST':
                        posts.append(request)
                        data = json.loads(request.content)
                        self.assertEqual(data, {'entity_id': command.device.entity_id, **args})
                        self.assertEqual(request.url.path, f'/api/services/{command.device.domain}/{service}')
                        current['state'] = final_state
                        for key, value in args.items():
                            current['attributes']['brightness' if key == 'brightness_pct' else key] = round(value*255/100) if key == 'brightness_pct' else value
                        return httpx.Response(200, json=[])
                    return httpx.Response(200, json=current)
                result = await ha.execute(command, CONFIG, transport=httpx.MockTransport(handler))
                self.assertEqual(len(posts), 1)
                self.assertNotIn('未确认', result); self.assertNotIn('尚未更新', result)

    async def test_state_query_does_not_write(self):
        requests = []
        async def handler(request):
            requests.append(request.method)
            return httpx.Response(200, json=state())
        result = await ha.execute(ha.parse('台灯开着吗', CONFIG), CONFIG, transport=httpx.MockTransport(handler))
        self.assertEqual(requests, ['GET'])
        self.assertIn('主卧台灯开着', result)

    async def test_timeout_after_post_is_not_retried(self):
        posts = []
        async def handler(request):
            if request.method == 'POST':
                posts.append(request); raise httpx.ReadTimeout('timeout')
            return httpx.Response(200, json=state())
        result = await ha.execute(ha.parse('关闭台灯', CONFIG), CONFIG, transport=httpx.MockTransport(handler))
        self.assertEqual(len(posts), 1); self.assertIn('结果未确认', result)

    async def test_errors_offline_capabilities_and_rejected_ids(self):
        for status, data in [(401, {}), (404, {}), (200, state(value='unavailable')),
                             (200, {'entity_id': 'light.wrong', 'state': 'on'}), (500, {})]:
            requests = []
            async def handler(request):
                requests.append(request.method); return httpx.Response(status, json=data)
            result = await ha.execute(ha.parse('打开台灯', CONFIG), CONFIG, transport=httpx.MockTransport(handler))
            self.assertEqual(requests, ['GET']); self.assertNotIn('已打开', result)
        unsupported = state(PLAYER); unsupported['attributes']['supported_features'] = 0
        async def handler(request):
            self.assertEqual(request.method, 'GET'); return httpx.Response(200, json=unsupported)
        result = await ha.execute(ha.parse('智慧屏静音', CONFIG), CONFIG, transport=httpx.MockTransport(handler))
        self.assertIn('不支持', result)
        fake = ha.Command(ha.Device('light.unlisted', '客厅', '灯', ('灯',)), 'turn_on')
        self.assertIn('没有配置', await ha.execute(fake, CONFIG))

    async def test_no_premature_success_and_no_post_after_interrupt(self):
        calls = []
        async def handler(request):
            calls.append(request.method)
            return httpx.Response(200, json=[] if request.method == 'POST' else state(value='off'))
        result = await ha.execute(ha.parse('打开台灯', CONFIG), CONFIG, transport=httpx.MockTransport(handler))
        self.assertIn('状态尚未更新', result); self.assertEqual(calls.count('POST'), 1)
        checks = [False, True]
        calls.clear()
        with self.assertRaises(ha.Superseded):
            await ha.execute(ha.parse('打开台灯', CONFIG), CONFIG, stale=lambda: checks.pop(0), transport=httpx.MockTransport(handler))
        self.assertEqual(calls, ['GET'])


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        from core.utils.dialogue import Dialogue
        return SimpleNamespace(config={'plugins': {'home_assistant': copy.deepcopy(CONFIG)}, 'selected_module': {'Intent': 'function_call'},
            'Intent': {'function_call': {'functions': ['hass_state']}}}, turn_gen=1,
            stop_event=threading.Event(), tts=SimpleNamespace(store_tts_text=MagicMock(), tts_one_sentence=MagicMock(), tts_text_queue=queue.Queue()),
            dialogue=Dialogue(), logger=MagicMock(), llm=MagicMock(), executor=MagicMock(), client_abort=False)

    async def test_direct_path_no_llm_and_no_retry_on_handled_error(self):
        for reply in ['主卧台灯已打开。', '操作结果未确认，请稍后查询状态。']:
            conn = self.connection()
            with patch.object(ha, 'execute', new_callable=AsyncMock, return_value=reply) as execute, \
                 patch('core.handle.sendAudioHandle.send_stt_message', new_callable=AsyncMock), \
                 patch('core.agent.chat_history.sync') as sync:
                self.assertTrue(await ha.handle(conn, '打开台灯', 1))
                execute.assert_awaited_once(); sync.assert_called_once()
                conn.llm.assert_not_called(); conn.executor.submit.assert_not_called()
                self.assertEqual(len(conn.dialogue.dialogue), 2)
                self.assertEqual(conn.dialogue.dialogue[-1].content, reply)
                self.assertEqual(conn.tts.tts_text_queue.qsize(), 2)

    async def test_fallback_disabled_ambiguous_json_and_stale(self):
        conn = self.connection()
        with patch.object(ha, 'execute', new_callable=AsyncMock) as execute:
            for text in ['把那个关了', '先开台灯然后关主灯']:
                self.assertFalse(await ha.handle(conn, text, 1))
            conn.config['plugins']['home_assistant']['direct_commands']['enabled'] = False
            self.assertFalse(await ha.handle(conn, '打开台灯', 1))
            execute.assert_not_awaited()
        conn = self.connection()
        with patch.object(ha, 'execute', new_callable=AsyncMock, return_value='主卧台灯开着。') as execute, \
             patch('core.handle.sendAudioHandle.send_stt_message', new_callable=AsyncMock), patch('core.agent.chat_history.sync'):
            self.assertTrue(await ha.handle(conn, json.dumps({'speaker': '测试', 'content': '台灯开着吗'}), 1))
            execute.assert_awaited_once()
        conn = self.connection(); conn.turn_gen = 2
        with patch.object(ha, 'execute', new_callable=AsyncMock) as execute:
            self.assertTrue(await ha.handle(conn, '打开台灯', 1)); execute.assert_not_awaited()

    async def test_actual_start_to_chat_router_skips_llm_only_on_match(self):
        conn = self.connection()
        conn.need_bind = False; conn.max_output_size = 0; conn.client_is_speaking = False
        conn.client_listen_mode = 'realtime'; conn.current_speaker = None
        conn.chat = MagicMock()
        async def identity(conn, text): return text
        with patch.object(receive, 'handle_call_name', side_effect=identity), \
             patch.object(receive.clear_command, 'handle', side_effect=identity), \
             patch.object(receive, 'handle_user_intent', new_callable=AsyncMock, return_value=False) as intent, \
             patch.object(receive, 'send_stt_message', new_callable=AsyncMock), \
             patch('core.handle.sendAudioHandle.send_stt_message', new_callable=AsyncMock), \
             patch('core.agent.chat_history.sync'), \
             patch.object(ha, 'execute', new_callable=AsyncMock, return_value='主卧台灯开着。') as execute:
            await receive.startToChat(conn, '台灯开着吗')
            execute.assert_awaited_once(); intent.assert_not_awaited(); conn.executor.submit.assert_not_called()
            await receive.startToChat(conn, '把刚才那个调暗一点')
            intent.assert_awaited_once(); conn.executor.submit.assert_called_once()

    async def test_plugin_returns_handled_reply_contract(self):
        from plugins_func.pre_llm import home_assistant
        conn = self.connection()
        with patch.object(ha, 'execute', new_callable=AsyncMock, return_value='主卧主灯已打开。') as execute:
            result = await home_assistant.intercept(conn, '主灯开')
            self.assertIs(result['handled'], True)
            self.assertEqual(result['reply'], '主卧主灯已打开。')
            self.assertEqual(result['entity'], SWITCH)
            execute.assert_awaited_once()
            execute.reset_mock()
            self.assertEqual(await home_assistant.intercept(conn, '明天天气怎么样'), {'handled': False})
            execute.assert_not_awaited()

    async def test_claimed_plugin_exception_never_reaches_model(self):
        from core.agent import pre_llm
        conn = self.connection()
        with patch.object(ha, 'execute', new_callable=AsyncMock, side_effect=RuntimeError('test')), \
             patch('core.handle.sendAudioHandle.send_stt_message', new_callable=AsyncMock), patch('core.agent.chat_history.sync'):
            self.assertTrue(await pre_llm.handle(conn, '主灯开', 1))
            self.assertIn('结果未确认', conn.dialogue.dialogue[-1].content)
            self.assertEqual(conn.llm.mock_calls, [])
            conn.executor.submit.assert_not_called()

    async def test_first_handled_plugin_short_circuits_registry(self):
        from core.agent import pre_llm
        conn = self.connection()
        first = SimpleNamespace(NAME='test', match=lambda *_: True,
                                intercept=AsyncMock(return_value={'handled': True, 'reply': '已开'}))
        second = SimpleNamespace(NAME='must-not-run', match=MagicMock(), intercept=AsyncMock())
        with patch.object(pre_llm, 'PLUGINS', (first, second)), \
             patch('core.handle.sendAudioHandle.send_stt_message', new_callable=AsyncMock), patch('core.agent.chat_history.sync'):
            self.assertTrue(await pre_llm.handle(conn, 'test', 1))
            first.intercept.assert_awaited_once(); second.match.assert_not_called()
            self.assertEqual(conn.dialogue.dialogue[-1].content, '已开')


if __name__ == '__main__': unittest.main()
