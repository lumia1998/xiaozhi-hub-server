"""Validated TTS presets. Preview is read-only; secrets are write-only and provider-scoped."""
import copy
import json
import math
import urllib.parse
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from core.admin import catalog, config_store

VOICE_PRESETS = json.loads(Path(__file__).with_name('tts_voice_presets.json').read_text(encoding='utf-8'))


def field(key, label, default='', kind='text', **extra):
    return {'key': key, 'label': label, 'default': default, 'kind': kind, **extra}


PROVIDERS = [
    {'id': 'edge', 'label': 'Edge TTS', 'entry': 'EdgeTTS', 'type': 'edge',
     'hint': '免费使用，无需密钥。',
     'fields': [field('voice', '音色', 'zh-CN-XiaoxiaoNeural', required=True,
                      choices=[{'value': v['value'], 'label': v['name'] + ' / ' + v['group']} for v in catalog.EDGE_VOICES]),
                field('rate', '语速调整（%）', 0, 'number', min=-90, max=100, step=1),
                field('pitch', '音调调整（Hz）', 0, 'number', min=-100, max=100, step=1)]},
    {'id': 'volcengine', 'label': '火山豆包', 'entry': 'HubVolcengineTTS', 'type': 'cloud_http',
     'hint': '填写火山语音控制台的 App ID、Access Token 和已开通的资源。',
     'fields': [field('appid', 'App ID', required=True), field('access_token', 'Access Token', kind='secret', required=True),
                field('resource_id', '资源 ID', 'seed-tts-2.0', required=True,
                      choices=[{'value': v, 'label': label} for v, label in [
                          ('seed-tts-2.0', '豆包语音合成 2.0（字符版）'),
                          ('seed-tts-1.0', '豆包语音合成 1.0（字符版）'),
                          ('seed-tts-1.0-concurr', '豆包语音合成 1.0（并发版）'),
                          ('seed-icl-2.0', '声音复刻 2.0（字符版）'),
                          ('seed-icl-1.0', '声音复刻 1.0（字符版）'),
                          ('seed-icl-1.0-concurr', '声音复刻 1.0（并发版）')]]),
                field('voice', '音色 ID', 'zh_female_xiaohe_uranus_bigtts', required=True,
                      hint='预设音色需匹配已开通的资源；复刻音色 ID 可直接填写。',
                      choices=VOICE_PRESETS['volcengine']),
                field('speech_rate', '语速调整', 0, 'number', min=-50, max=100, step=1),
                field('loudness_rate', '音量调整', 0, 'number', min=-50, max=100, step=1),
                field('emotion', '情感', hint='可选，需音色支持。',
                      choices=[{'value': v, 'label': label} for v, label in [
                          ('', '默认'), ('neutral', '中性'), ('happy', '开心 / 愉悦'), ('sad', '悲伤'),
                          ('angry', '生气 / 愤怒'), ('surprised', '惊讶'), ('fear', '恐惧'), ('hate', '厌恶'),
                          ('excited', '激动 / 兴奋'), ('coldness', '冷漠'), ('depressed', '沮丧'),
                          ('lovey-dovey', '撒娇'), ('shy', '害羞'), ('comfort', '安慰鼓励'),
                          ('tension', '咆哮 / 焦急'), ('tender', '温柔'), ('storytelling', '讲故事'),
                          ('radio', '情感电台'), ('magnetic', '磁性'), ('advertising', '广告营销'),
                          ('vocal-fry', '气泡音'), ('news', '新闻播报'), ('entertainment', '娱乐八卦'),
                          ('chat', '对话 / 闲聊'), ('asmr', '低语 / ASMR'), ('warm', '温暖'),
                          ('affectionate', '深情'), ('authoritative', '权威')]]),
                field('language', '语言', hint='可选，例如 zh。'),
                field('api_url', '接口地址', 'https://openspeech.bytedance.com/api/v3/tts/unidirectional', 'url', required=True)]},
    {'id': 'minimax', 'label': '海螺 / MiniMax', 'entry': 'HubMiniMaxTTS', 'type': 'cloud_http',
     'hint': '使用 MiniMax 开放平台 API Key，支持自定义及复刻音色 ID。',
     'fields': [field('api_key', 'API Key', kind='secret', required=True), field('group_id', 'Group ID', hint='可选，旧版账号需要时填写。'),
                field('model', '模型', 'speech-2.6-turbo', required=True,
                      choices=[{'value': v, 'label': v} for v in ['speech-2.8-hd', 'speech-2.8-turbo', 'speech-2.6-hd', 'speech-2.6-turbo', 'speech-02-hd', 'speech-02-turbo']]),
                field('voice', '音色 ID', 'female-tianmei', required=True,
                      choices=VOICE_PRESETS['minimax']),
                field('speed', '语速', 1, 'number', min=.5, max=2, step=.1),
                field('volume', '音量', 1, 'number', min=.1, max=10, step=.1),
                field('pitch', '音调', 0, 'number', min=-12, max=12, step=1),
                field('emotion', '情感', hint='可选，需模型和音色支持。',
                      choices=[{'value': v, 'label': label} for v, label in [
                          ('', '自动'), ('happy', '开心'), ('sad', '悲伤'), ('angry', '愤怒'),
                          ('fearful', '恐惧'), ('disgusted', '厌恶'), ('surprised', '惊讶'),
                          ('calm', '平静'), ('fluent', '流畅（2.6+）'), ('whisper', '耳语（2.6+）')]]),
                field('language', '语言增强', 'auto', choices=[{'value': v, 'label': v} for v in ['auto', 'Chinese', 'Chinese,Yue', 'English']]),
                field('api_url', '接口地址', 'https://api.minimaxi.com/v1/t2a_v2', 'url', required=True)]},
    {'id': 'openai', 'label': 'OpenAI 兼容', 'entry': 'HubOpenAITTS', 'type': 'cloud_http',
     'hint': '适用于 OpenAI、兼容代理及本地语音服务。填写完整的语音合成接口地址。',
     'fields': [field('api_url', '接口地址', 'https://api.openai.com/v1/audio/speech', 'url', required=True),
                field('api_key', 'API Key', kind='secret', hint='本地免认证服务可留空。'),
                field('model', '模型', 'tts-1', required=True),
                field('voice', '音色 ID', 'alloy', required=True),
                field('speed', '语速', 1, 'number', min=.25, max=4, step=.05),
                field('instructions', '声音风格', hint='可选，仅支持 instructions 的模型生效。', max_length=1000)]},
]
BY_ID = {p['id']: p for p in PROVIDERS}
SAMPLE = '你好，我是小葵。今天想聊些什么？'


def _preset(provider):
    if provider not in BY_ID: raise ValueError('请选择支持的语音服务')
    return BY_ID[provider]


def _entry(preset, merged):
    # Respect installations using a custom Edge entry name.
    name = preset['entry']
    selected = (merged.get('selected_module') or {}).get('TTS')
    if preset['id'] == 'edge' and ((merged.get('TTS') or {}).get(selected) or {}).get('type') == 'edge': name = selected
    entry = copy.deepcopy((merged.get('TTS') or {}).get(name) or {})
    if entry and (entry.get('type') != preset['type'] or (preset['type'] == 'cloud_http' and entry.get('provider') != preset['id'])):
        raise ValueError(f'{name} 已被其他类型占用，请先检查配置')
    return name, entry


def _value(entry, f):
    value = entry.get(f['key'], f['default'])
    if config_store.is_placeholder(value) or value == '': return f['default']
    return value


def describe(merged):
    selected = (merged.get('selected_module') or {}).get('TTS')
    options = []
    for p in PROVIDERS:
        try:
            name, entry = _entry(p, merged)
            conflict = False
        except ValueError:
            name, entry, conflict = p['entry'], {}, True
        fields = []
        for f in p['fields']:
            value = _value(entry, f)
            fields.append({**f, 'value': '' if f['kind'] == 'secret' else value,
                           **({'configured': bool(value)} if f['kind'] == 'secret' else {})})
        configured = not conflict and all(bool(_value(entry, f)) for f in p['fields'] if f.get('required'))
        options.append({'id': p['id'], 'label': p['label'], 'entry': name, 'hint': p['hint'],
                        'fields': fields, 'configured': configured, 'selected': name == selected, 'conflict': conflict})
    return {'providers': options, 'provider': next((p['id'] for p in options if p['selected']), None), 'sample_text': SAMPLE}


def prepare(provider, payload, merged):
    p = _preset(provider)
    if not isinstance(payload, dict): raise ValueError('配置格式不正确')
    fields = {f['key']: f for f in p['fields']}
    if set(payload) - fields.keys(): raise ValueError('配置包含未知字段')
    name, old = _entry(p, merged)
    result = copy.deepcopy(old)
    result['type'] = p['type']
    if p['type'] == 'cloud_http': result['provider'] = provider
    result.setdefault('output_dir', 'tmp/')
    for key, f in fields.items():
        value = payload.get(key, _value(old, f))
        if f['kind'] == 'secret' and value == '': value = _value(old, f)
        if f['kind'] == 'number':
            if isinstance(value, bool) or not isinstance(value, (str, int, float)): raise ValueError(f"{f['label']}需要是数字")
            try: value = float(value)
            except (ValueError, TypeError): raise ValueError(f"{f['label']}需要是数字") from None
            if not math.isfinite(value) or not f['min'] <= value <= f['max']:
                raise ValueError(f"{f['label']}范围为 {f['min']} 到 {f['max']}")
            if f['step'] == 1:
                if not value.is_integer(): raise ValueError(f"{f['label']}需要是整数")
                value = int(value)
        else:
            if not isinstance(value, str): raise ValueError(f"{f['label']}需要是文本")
            value = value.strip()
            if len(value) > f.get('max_length', 4096 if f['kind'] == 'secret' else 2048) or any(ord(c) < 32 for c in value):
                raise ValueError(f"{f['label']}格式不正确")
            if f['kind'] == 'secret' and any(c.isspace() for c in value): raise ValueError('密钥不能包含空格')
            if f['kind'] == 'url':
                try:
                    url = urllib.parse.urlsplit(value)
                    valid = (url.scheme in ('https', 'http') and url.hostname and not url.username and not url.password
                             and not url.fragment and not url.query and '\\' not in value and not any(c.isspace() for c in value)
                             and (url.port is None or 0 < url.port < 65536))
                except ValueError: valid = False
                if not valid: raise ValueError('接口地址需要是完整的 HTTP 或 HTTPS URL，不含账号、查询参数或片段')
        if f.get('required') and (value == '' or config_store.is_placeholder(value)):
            raise ValueError(f"请填写{f['label']}")
        result[key] = value
    # Prevent silently sending a stored key to a different endpoint.
    if old.get('api_url') and result.get('api_url') != old['api_url']:
        for key, f in fields.items():
            if f['kind'] == 'secret' and _value(old, f) and not payload.get(key):
                raise ValueError('修改接口地址后请重新填写密钥')
    # Edited voice must be the voice used; older private_voice overrides would otherwise win.
    result.pop('private_voice', None)
    if provider == 'edge': result['format'] = 'mp3'
    else: result['tts_timeout'] = 30
    return name, result


def save(provider, payload, merged, path=None):
    # edit_custom serializes writes and preserves comments, keys, unrelated fields and selection.
    with config_store.edit_custom(path) as custom:
        latest = copy.deepcopy(merged)
        from config.config_loader import merge_configs
        latest = merge_configs(latest, custom)
        name, candidate = prepare(provider, payload, latest)
        old = (latest.get('TTS') or {}).get(name) or {}
        changed = [k for k in candidate if candidate[k] != old.get(k)]
        if old.get('private_voice'): changed.append('private_voice')
        custom.setdefault('TTS', CommentedMap())[name] = CommentedMap(candidate)
        if (latest.get('selected_module') or {}).get('TTS') != name: changed.append('selected')
        custom.setdefault('selected_module', CommentedMap())['TTS'] = name
    return name, changed


async def preview(provider, values, merged, text):
    import asyncio
    import importlib
    name, candidate = prepare(provider, values, merged)
    cls = importlib.import_module('core.providers.tts.' + candidate['type']).TTSProvider
    tts = cls(candidate, True)
    try:
        audio = await asyncio.wait_for(tts.text_to_speak(text, None), timeout=32)
    except asyncio.TimeoutError:
        raise ValueError('试听合成超时，请检查接口和网络') from None
    if not audio: raise ValueError('语音服务没有返回音频')
    return audio
