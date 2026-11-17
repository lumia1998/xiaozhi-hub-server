"""Provider wire contracts and real admin HTTP routes, using local synthetic audio only."""
import asyncio
import base64
import copy
import io
import json
import math
import pathlib
import struct
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin import config_store, tts_settings as settings
from core.admin.admin_handler import AdminHandler, COOKIE_NAME
from core.providers.tts.cloud_http import TTSProvider, SynthesisError
from core.utils import tts_phrase_cache


BASE = {'selected_module': {'TTS': 'EdgeTTS', 'LLM': 'existing-model'},
        'TTS': {'EdgeTTS': {'type': 'edge', 'voice': 'zh-CN-XiaoxiaoNeural', 'output_dir': 'tmp/'}},
        'plugins': {'home_assistant': {'direct_commands': {'enabled': True}}}, 'prompt': '你叫小葵。'}


def values(provider):
    p = settings.BY_ID[provider]
    result = {f['key']: f['default'] for f in p['fields']}
    for f in p['fields']:
        if f['kind'] == 'secret': result[f['key']] = 'test-secret-never-return'
    if provider == 'volcengine': result['appid'] = 'test-app'
    return result


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / 'config.yaml'
        self.path.write_text('# preserve this comment\n' + __import__('yaml').safe_dump(BASE, allow_unicode=True), encoding='utf-8')

    def merged(self): return __import__('yaml').safe_load(self.path.read_text(encoding='utf-8'))

    def test_describe_masks_secrets_and_keeps_config(self):
        for provider in settings.BY_ID:
            settings.save(provider, values(provider), self.merged(), self.path)
        snapshot = settings.describe(self.merged())
        self.assertEqual(len(snapshot['providers']), 4)
        self.assertNotIn('test-secret-never-return', json.dumps(snapshot))
        self.assertEqual(self.merged()['plugins'], BASE['plugins'])
        self.assertEqual(self.merged()['prompt'], BASE['prompt'])
        self.assertIn('# preserve this comment', self.path.read_text(encoding='utf-8'))

    def test_save_secret_blank_preserves_and_changes_selection(self):
        name, changed = settings.save('minimax', values('minimax'), self.merged(), self.path)
        self.assertEqual(name, 'HubMiniMaxTTS'); self.assertIn('selected', changed)
        settings.save('minimax', {'api_key': '', 'voice': 'custom-clone'}, self.merged(), self.path)
        e = self.merged()['TTS'][name]
        self.assertEqual(e['api_key'], 'test-secret-never-return'); self.assertEqual(e['voice'], 'custom-clone')
        self.assertEqual(e['provider'], 'minimax')

    def test_prepare_never_persists_and_switch_requires_credentials(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): settings.save('minimax', {'voice': 'custom'}, self.merged(), self.path)
        self.assertEqual(before, self.path.read_bytes())
        settings.prepare('volcengine', values('volcengine'), self.merged())
        self.assertEqual(before, self.path.read_bytes())

    def test_endpoint_change_requires_reentering_secret(self):
        settings.save('minimax', values('minimax'), self.merged(), self.path)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '重新填写'):
            settings.save('minimax', {'api_url': 'https://another.example/v1/tts'}, self.merged(), self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_invalid_values_do_not_write(self):
        for field, value in [('speed', 'nan'), ('speed', True), ('speed', 3), ('pitch', .5),
                             ('voice', ''), ('api_key', 'secret key'), ('api_url', 'file:///etc/passwd'),
                             ('api_url', 'https://user:pass@example.test/tts'), ('api_url', 'https://example.test:99999'),
                             ('api_url', 'https://example.test/tts?key=x'), ('other', 'anything')]:
            with self.subTest(field=field, value=value):
                before = self.path.read_bytes()
                with self.assertRaises(ValueError): settings.save('minimax', {**values('minimax'), field: value}, self.merged(), self.path)
                self.assertEqual(before, self.path.read_bytes())

    def test_conflicting_entry_not_overwritten(self):
        base = copy.deepcopy(BASE); base['TTS']['HubMiniMaxTTS'] = {'type': 'edge', 'voice': 'custom'}
        with self.assertRaises(ValueError): settings.prepare('minimax', values('minimax'), base)

    def test_cache_changes_with_model_emotion_speed_and_url(self):
        _, c = settings.prepare('minimax', values('minimax'), BASE)
        first = tts_phrase_cache.key(TTSProvider(c, True), '你好', 16000)
        for key, value in [('model', 'other'), ('emotion', 'happy'), ('speed', 1.2), ('api_url', 'https://other.test')]:
            candidate = TTSProvider({**c, key: value}, True)
            self.assertNotEqual(first, tts_phrase_cache.key(candidate, '你好', 16000))


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        raw = io.BytesIO()
        with wave.open(raw, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
            wav.writeframes(b''.join(struct.pack('<h', int(1200 * math.sin(2 * math.pi * 440 * n / 24000))) for n in range(6000)))
        cls.audio = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', 'pipe:0', '-f', 'mp3', 'pipe:1'], input=raw.getvalue())

    async def fixture(self, handler):
        app = web.Application(); app.router.add_post('/tts', handler)
        server = TestServer(app); await server.start_server(); self.addAsyncCleanup(server.close)
        return str(server.make_url('/tts'))

    def provider(self, kind, url, **override):
        _, cfg = settings.prepare(kind, {**values(kind), 'api_url': url, **override}, BASE)
        return TTSProvider(cfg, True)

    async def test_volcengine_v3_fragmented_ndjson_and_file_output(self):
        async def handler(request):
            self.assertEqual(request.headers['X-Api-App-Id'], 'test-app')
            self.assertEqual(request.headers['X-Api-Access-Key'], 'test-secret-never-return')
            self.assertEqual(request.headers['X-Api-Resource-Id'], 'seed-tts-2.0')
            body = await request.json(); params = body['req_params']
            self.assertEqual(params['speaker'], 'custom-voice')
            self.assertEqual(params['audio_params']['emotion'], 'happy')
            self.assertEqual(json.loads(params['additions']), {'explicit_language': 'zh'})
            stream = web.StreamResponse(headers={'Content-Type': 'application/json'}); await stream.prepare(request)
            payload = (json.dumps({'code': 0, 'data': base64.b64encode(self.audio).decode()}) + '\r\n' + json.dumps({'code': 20000000})).encode()
            for start in range(0, len(payload), 17): await stream.write(payload[start:start+17])
            await stream.write_eof(); return stream
        url = await self.fixture(handler)
        tts = self.provider('volcengine', url, voice='custom-voice', emotion='happy', language='zh')
        self.assertEqual(await tts.text_to_speak('你好', None), self.audio)
        with tempfile.TemporaryDirectory() as tmp:
            file = pathlib.Path(tmp) / 'tts.mp3'; await tts.text_to_speak('你好', file)
            self.assertEqual(file.read_bytes(), self.audio)
        pcm = await asyncio.to_thread(tts.synthesize_pcm, '兼容合成测试', 24000)
        self.assertGreater(len(pcm), 10000)

    async def test_minimax_request_and_decode(self):
        async def handler(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer test-secret-never-return')
            self.assertEqual(request.query['GroupId'], '123')
            body = await request.json()
            self.assertFalse(body['stream']); self.assertEqual(body['output_format'], 'hex')
            self.assertEqual(body['voice_setting'], {'voice_id': 'clone123', 'speed': 1.2, 'vol': 1.4, 'pitch': -2, 'emotion': 'happy'})
            self.assertEqual(body['audio_setting'], {'format': 'mp3', 'sample_rate': 24000, 'channel': 1})
            return web.json_response({'base_resp': {'status_code': 0}, 'data': {'audio': self.audio.hex()}})
        url = await self.fixture(handler)
        tts = self.provider('minimax', url, group_id='123', voice='clone123', speed=1.2, volume=1.4, pitch=-2, emotion='happy')
        self.assertEqual(await tts.text_to_speak('你好', None), self.audio)

    async def test_openai_compatible_payload(self):
        async def handler(request):
            body = await request.json()
            self.assertEqual(body, {'input': '你好', 'model': 'local-model', 'voice': 'custom', 'speed': 1.1,
                                    'response_format': 'mp3', 'instructions': '温和自然'})
            return web.Response(body=self.audio, content_type='audio/mpeg')
        tts = self.provider('openai', await self.fixture(handler), model='local-model', voice='custom', speed=1.1, instructions='温和自然')
        self.assertEqual(await tts.text_to_speak('你好', None), self.audio)

    async def test_error_responses_never_leak_key_or_raw_body(self):
        cases = [('minimax', 200, {'base_resp': {'status_code': 1004, 'status_msg': 'test-secret-never-return'}}),
                 ('minimax', 200, {'base_resp': {'status_code': 0}, 'data': {'audio': 'xyz'}}),
                 ('minimax', 200, {'base_resp': {'status_code': 0}, 'data': {'audio': ''}}),
                 ('volcengine', 200, {'code': 400, 'message': 'test-secret-never-return'}),
                 ('volcengine', 200, {'code': 0, 'data': base64.b64encode(self.audio).decode()}),
                 ('openai', 401, {'error': 'test-secret-never-return'}), ('openai', 200, {'error': 'bad'})]
        for kind, status, body in cases:
            async def handler(_request): return web.json_response(body, status=status)
            tts = self.provider(kind, await self.fixture(handler))
            with self.subTest(kind=kind, status=status), self.assertRaises(SynthesisError) as error:
                await tts.text_to_speak('test', None)
            self.assertNotIn('test-secret', str(error.exception))

    async def test_timeout_does_not_block_event_loop(self):
        loop = asyncio.get_running_loop()
        def later(seconds):  # tests/conftest.py 把 asyncio.sleep 换成了立即返回，这里用真的定时器
            done = loop.create_future(); loop.call_later(seconds, lambda: done.done() or done.set_result(None)); return done
        async def handler(_request):
            await later(.1); return web.Response(body=self.audio)
        tts = self.provider('openai', await self.fixture(handler)); tts.tts_timeout = .02
        marker = []
        async def tick(): await later(.005); marker.append(True)
        tick_task = asyncio.create_task(tick())
        with self.assertRaises(SynthesisError): await tts.text_to_speak('test', None)
        self.assertTrue(marker); await tick_task  # 等响应的这段时间事件循环照常在跑


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / '.config.yaml'
        self.path.write_text(__import__('yaml').safe_dump(BASE, allow_unicode=True), encoding='utf-8')
        self.handler = AdminHandler(copy.deepcopy(BASE), config_path=self.path)
        self.app = web.Application(); self.handler.register(self.app)
        self.client = TestClient(TestServer(self.app)); await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.cookie = {COOKIE_NAME: self.handler.sessions.create()}

    async def test_auth_and_schema(self):
        self.assertEqual((await self.client.post('/admin/api/tts/preview', json={})).status, 401)
        self.assertEqual((await self.client.post('/admin/api/tts', json={})).status, 401)
        r = await self.client.get('/admin/api/tts', cookies=self.cookie); data = await r.json()
        self.assertEqual(len(data['providers']), 4)
        for name in ['tts-page.js', 'tts-page.css']:
            self.assertEqual((await self.client.get('/admin/assets/' + name)).status, 200)
        self.assertEqual((await self.client.get('/admin/assets/auth.py')).status, 404)

    async def test_save_switches_runtime_and_persistence_without_leaking_secret(self):
        r = await self.client.post('/admin/api/tts', json={'provider': 'minimax', 'values': values('minimax')}, cookies=self.cookie)
        data = await r.json(); self.assertEqual(r.status, 200, data)
        self.assertEqual(self.handler.config['selected_module']['TTS'], 'HubMiniMaxTTS')
        self.assertEqual(config_store.load_merged(custom_path=self.path)['selected_module']['TTS'], 'HubMiniMaxTTS')
        r = await self.client.get('/admin/api/tts', cookies=self.cookie)
        self.assertNotIn('test-secret-never-return', await r.text())
        before = self.path.read_bytes()
        r = await self.client.post('/admin/api/tts', json={'provider': 'volcengine', 'values': {}}, cookies=self.cookie)
        self.assertEqual(r.status, 400); self.assertEqual(before, self.path.read_bytes())

    async def test_preview_current_form_does_not_save_and_returns_audio(self):
        original = self.path.read_bytes()
        async def synth(provider, value, merged, text):
            self.assertEqual(value['voice'], 'custom-clone'); self.assertEqual(text, '你好小葵')
            return b'ID3-test-audio'
        with patch.object(settings, 'preview', side_effect=synth):
            r = await self.client.post('/admin/api/tts/preview', json={'provider': 'minimax',
                'values': {**values('minimax'), 'voice': 'custom-clone'}, 'text': '你好小葵'}, cookies=self.cookie)
            self.assertEqual(r.status, 200); self.assertEqual(await r.read(), b'ID3-test-audio')
            self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(self.handler.config['selected_module']['TTS'], 'EdgeTTS')

    async def test_preview_error_does_not_leak_and_text_limit(self):
        with patch.object(settings, 'preview', side_effect=RuntimeError('secret-private-key')):
            r = await self.client.post('/admin/api/tts/preview', json={'provider': 'edge', 'values': {}}, cookies=self.cookie)
            self.assertEqual(r.status, 502); self.assertNotIn('secret-private-key', await r.text())
        r = await self.client.post('/admin/api/tts/preview', json={'provider': 'edge', 'text': 'a' * 201}, cookies=self.cookie)
        self.assertEqual(r.status, 400)


if __name__ == '__main__': unittest.main()
