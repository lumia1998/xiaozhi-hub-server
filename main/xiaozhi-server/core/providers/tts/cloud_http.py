"""Sentence synthesis adapters compatible with the Hub's PCM/Opus and interruption pipeline.

API contracts cross-checked against media-luna's volcengine-tts, minimax and openai-tts
connectors: Volcengine V3 NDJSON/base64, MiniMax T2A V2 JSON/hex, OpenAI audio/speech.
No credentials or upstream bodies are included in exceptions or logs.
"""
import asyncio
import base64
import binascii
import hashlib
import json
import pathlib
import uuid

import aiohttp

from core.providers.tts.base import TTSProviderBase

MAX_AUDIO = 20 * 1024 * 1024
MAX_RESPONSE = 48 * 1024 * 1024


class SynthesisError(RuntimeError):
    pass


def business_error(provider, code):
    safe = str(code) if isinstance(code, int) and not isinstance(code, bool) else 'unknown'
    return SynthesisError(f'{provider} 合成失败，服务代码 {safe}；请检查密钥、模型及音色权限')


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.settings = dict(config)
        self.provider = config['provider']
        if self.provider not in ('volcengine', 'minimax', 'openai'):
            raise ValueError('不支持这个语音服务')
        self.api_url = config['api_url']
        self.voice = config['voice']
        self.audio_file_type = 'mp3'
        # Include model, URL, emotion, speed, pitch and credentials, not just the voice name.
        self.cache_identity = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def request_parts(self, text):
        c = self.settings
        if self.provider == 'volcengine':
            headers = {'X-Api-App-Id': c['appid'], 'X-Api-Access-Key': c['access_token'],
                       'X-Api-Resource-Id': c['resource_id'], 'X-Api-Request-Id': str(uuid.uuid4())}
            audio = {'format': 'mp3', 'sample_rate': 24000, 'speech_rate': c.get('speech_rate', 0),
                     'loudness_rate': c.get('loudness_rate', 0)}
            if c.get('emotion'): audio['emotion'] = c['emotion']
            request = {'text': text, 'speaker': self.voice, 'audio_params': audio}
            if c.get('language'):
                request['additions'] = json.dumps({'explicit_language': c['language']})
            return headers, {'user': {'uid': 'xiaozhi-hub'}, 'req_params': request}
        headers = {'Authorization': 'Bearer ' + c['api_key']} if c.get('api_key') else {}
        if self.provider == 'minimax':
            voice = {'voice_id': self.voice, 'speed': c.get('speed', 1), 'vol': c.get('volume', 1), 'pitch': c.get('pitch', 0)}
            if c.get('emotion'): voice['emotion'] = c['emotion']
            return headers, {'model': c['model'], 'text': text, 'stream': False, 'output_format': 'hex',
                             'language_boost': c.get('language') or 'auto', 'voice_setting': voice,
                             'audio_setting': {'sample_rate': 24000, 'format': 'mp3', 'channel': 1}}
        payload = {'model': c['model'], 'input': text, 'voice': self.voice,
                   'response_format': 'mp3', 'speed': c.get('speed', 1)}
        if c.get('instructions'): payload['instructions'] = c['instructions']
        return headers, payload

    async def _body(self, response):
        parts, size = [], 0
        async for part in response.content.iter_chunked(65536):
            size += len(part)
            if size > MAX_RESPONSE: raise SynthesisError('语音服务响应过大')
            parts.append(part)
        return b''.join(parts)

    async def _volcengine(self, response):
        audio, buffer, size = bytearray(), bytearray(), 0
        complete = False
        def consume(line):
            nonlocal complete
            if not line.strip(): return
            data = json.loads(line)
            if not isinstance(data, dict): raise ValueError()
            code = data.get('code')
            if code not in (0, 20000000): raise business_error('火山', code)
            if code == 0 and data.get('data'):
                audio.extend(base64.b64decode(data['data'], validate=True))
                if len(audio) > MAX_AUDIO: raise SynthesisError('合成音频过大')
            if code == 20000000: complete = True
        async for part in response.content.iter_chunked(65536):
            size += len(part)
            if size > MAX_RESPONSE: raise SynthesisError('语音服务响应过大')
            buffer.extend(part)
            while b'\n' in buffer:
                line, _, rest = buffer.partition(b'\n'); buffer = bytearray(rest)
                consume(line)
            if complete: break
        if buffer and not complete: consume(buffer)
        if not complete: raise SynthesisError('火山语音流提前结束，请重试')
        return bytes(audio)

    async def text_to_speak(self, text, output_file):
        headers, payload = self.request_parts(text)
        params = {'GroupId': self.settings['group_id']} if self.provider == 'minimax' and self.settings.get('group_id') else None
        timeout = aiohttp.ClientTimeout(total=self.tts_timeout, connect=min(5, self.tts_timeout))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.api_url, headers=headers, json=payload, params=params, allow_redirects=False) as response:
                    if response.status != 200:
                        raise SynthesisError(f'语音服务返回 HTTP {response.status}，请检查接口和授权')
                    if self.provider == 'volcengine':
                        audio = await self._volcengine(response)
                    elif self.provider == 'minimax':
                        data = json.loads(await self._body(response))
                        code = (data.get('base_resp') or {}).get('status_code')
                        if code != 0: raise business_error('MiniMax', code)
                        encoded = (data.get('data') or {}).get('audio')
                        if not isinstance(encoded, str) or len(encoded) > MAX_AUDIO * 2: raise ValueError()
                        audio = bytes.fromhex(encoded)
                    else:
                        if 'json' in response.headers.get('Content-Type', '').lower():
                            raise SynthesisError('语音服务返回了 JSON，未返回音频')
                        audio = await self._body(response)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            raise SynthesisError('语音服务连接失败或超时，请检查网络和接口地址') from None
        except (ValueError, TypeError, KeyError, AttributeError, binascii.Error):
            raise SynthesisError('语音服务返回的数据格式无效') from None
        if not audio: raise SynthesisError('语音服务没有返回音频')
        if len(audio) > MAX_AUDIO: raise SynthesisError('合成音频过大')
        if output_file:
            pathlib.Path(output_file).write_bytes(audio)
            return None
        return audio
