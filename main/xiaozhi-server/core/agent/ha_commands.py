"""Exact, single-device HA commands before the LLM; uncertain language falls through.

Enabled by plugins.home_assistant.direct_commands.enabled. Entity IDs must be present
in that plugin's devices list. No toggles, retries, inferred targets, or substring matching.
"""
import asyncio
import re
import unicodedata
from dataclasses import dataclass

import httpx

TAG = __name__
ENTITY = re.compile(r'(?:light|switch|media_player)\.[a-z0-9_]+')
NUMBER = r'(?:\d{1,3}|[零〇一二两三四五六七八九十百]{1,4})'
PERCENT = rf'(?:百分之{NUMBER}|{NUMBER}%|{NUMBER})'
ADDRESS = re.compile(r'^(?:小葵小葵|哈喽小葵|小葵同学|小葵)')
POLITE = re.compile(r'^(?:请帮我|麻烦你帮我|麻烦帮我|帮我|请|麻烦你|麻烦|给我)')
SUFFIX = re.compile(r'(?:一下吧|一下|好吗|好不好|可以吗|吧)$')


@dataclass(frozen=True)
class Device:
    entity_id: str
    room: str
    name: str
    aliases: tuple

    @property
    def domain(self):
        return self.entity_id.split('.')[0]

    @property
    def label(self):
        return self.room + self.name


@dataclass(frozen=True)
class Command:
    device: Device
    action: str
    value: int | None = None


def normalize(text):
    if not isinstance(text, str) or len(text) > 100:
        return ''
    text = unicodedata.normalize('NFKC', text).lower().strip()
    # Do not strip quotes, negation, decimal points or conditional clauses.
    return re.sub(r'[\s，,。！？!?、；;：:]', '', text).rstrip('.')


def percent(text):
    text = text.removeprefix('百分之').removesuffix('%')
    digits = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if text.isascii() and text.isdecimal():
        value = int(text)
    elif text in ('一百', '百'):
        value = 100
    elif text in digits:
        value = digits[text]
    elif text.count('十') == 1:
        tens, ones = text.split('十')
        if tens and (tens not in digits or digits[tens] == 0): return None
        if ones and (ones not in digits or digits[ones] == 0): return None
        value = (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
    else:
        return None
    return value if 0 <= value <= 100 else None


def devices(ha):
    rows = ha.get('devices') or []
    if isinstance(rows, str): rows = rows.splitlines()
    extra = (ha.get('direct_commands') or {}).get('aliases') or {}
    found = {}
    for row in rows:
        if not isinstance(row, str): continue
        parts = [p.strip() for p in re.split('[,，]', row)]
        if len(parts) != 3 or not ENTITY.fullmatch(parts[2]) or not parts[1]: continue
        room, name, entity = parts
        aliases = {normalize(name), normalize(room + name), normalize(room + '的' + name)}
        for alias in extra.get(entity, []):
            if isinstance(alias, str) and 1 < len(alias) <= 30:
                aliases.add(normalize(alias))
        if entity in found: aliases.update(found[entity].aliases)
        found[entity] = Device(entity, room, name, tuple(sorted(aliases - {''})))
    return tuple(found.values())


def parse(text, ha):
    t = normalize(text)
    t = ADDRESS.sub('', t, count=1)
    t = POLITE.sub('', t, count=1)
    t = re.sub(r'^(?:把|将)', '', t, count=1)
    t = SUFFIX.sub('', t, count=1)
    matches = set()
    for device in devices(ha):
        for alias in device.aliases:
            a = re.escape(alias)
            patterns = [
                ('state', rf'(?:查询|查看|查一下|看看|看一下){a}(?:的)?(?:状态)?'),
                ('state', rf'{a}(?:的)?(?:状态|现在的状态|是什么状态|现在是什么状态|开着吗|关着吗|有没有开|是否开着|亮着吗|现在开着吗)'),
                ('state', rf'{a}(?:的)?(?:亮度|音量)(?:是多少|有多大|多大|多少)'),
            ]
            if device.domain in ('light', 'switch', 'media_player'):
                patterns += [('turn_on', rf'(?:(?:打开|开启|开){a}|{a}(?:打开|开启|开灯|开))'),
                             ('turn_off', rf'(?:(?:关闭|关掉|关上|关){a}|{a}(?:关闭|关掉|关上|关灯|关))')]
            if device.domain == 'light':
                patterns += [('brightness_up', rf'{a}(?:调亮|亮)(?:一点|一些)|(?:调亮){a}'),
                             ('brightness_down', rf'{a}(?:调暗|暗)(?:一点|一些)|(?:调暗){a}'),
                             ('brightness', rf'{a}(?:的)?(?:亮度)?(?:调到|调成|设为|设置为|调至)({PERCENT})')]
            if device.domain == 'media_player':
                patterns += [('volume', rf'{a}(?:的)?音量(?:调到|调成|设为|设置为|调至)({PERCENT})'),
                             ('volume_up', rf'{a}(?:的)?(?:音量调大|声音调大|声音大|音量大)(?:一点|一些)'),
                             ('volume_down', rf'{a}(?:的)?(?:音量调小|声音调小|声音小|音量小)(?:一点|一些)'),
                             ('pause', rf'(?:暂停{a}(?:的)?(?:播放)?|{a}暂停(?:播放)?)'),
                             ('stop', rf'(?:(?:停止|停下|停){a}(?:播放)?|{a}(?:停止播放|停止|停下|停))'),
                             ('play', rf'(?:继续播放{a}|{a}继续播放)'),
                             ('mute', rf'(?:静音{a}|{a}静音)'),
                             ('unmute', rf'(?:取消{a}(?:的)?静音|{a}取消静音)')]
            for action, pattern in patterns:
                match = re.fullmatch(pattern, t)
                if not match: continue
                value = percent(match.group(1)) if action in ('brightness', 'volume') else None
                if action in ('brightness', 'volume') and value is None: continue
                matches.add(Command(device, action, value))
    # Duplicate short names across rooms must remain for the model to disambiguate.
    return next(iter(matches)) if len(matches) == 1 else None


class Superseded(Exception):
    pass


class ExecutionResult(str):
    """String-compatible response carrying the verified outcome from HA state."""
    def __new__(cls, reply, confirmed=False):
        value = super().__new__(cls, reply)
        value.confirmed = bool(confirmed)
        return value


def _outcome(reply, confirmed=False):
    return ExecutionResult(reply, confirmed)


def _fresh(check):
    if check(): raise Superseded()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def state_reply(device, data):
    state = data.get('state')
    labels = {'on': '开着', 'off': '关着', 'playing': '正在播放', 'paused': '已暂停',
              'idle': '处于待机状态', 'standby': '处于待机状态', 'unavailable': '暂时离线', 'unknown': '状态暂时未知'}
    parts = [device.label + labels.get(state, '状态暂时无法识别')]
    attrs = data.get('attributes') or {}
    if device.domain == 'light' and state == 'on' and _number(attrs.get('brightness')):
        parts.append(f"亮度百分之{round(attrs['brightness'] / 255 * 100)}")
    if device.domain == 'media_player' and _number(attrs.get('volume_level')):
        parts.append(f"音量百分之{round(attrs['volume_level'] * 100)}")
    return '，'.join(parts) + '。'


def service_call(command, current):
    """Return (service, payload, expected-state, spoken-success), or a definite limitation."""
    d, action = command.device, command.action
    attrs = current.get('attributes') or {}
    data = {'entity_id': d.entity_id}
    expected = {}
    if action in ('turn_on', 'turn_off'):
        if d.domain == 'media_player':
            feature = 128 if action == 'turn_off' else 256
            supported = attrs.get('supported_features', 0)
            if not isinstance(supported, int) or not supported & feature:
                return f'{d.label}不支持这项操作。'
        service = action
        expected['state'] = 'on' if action == 'turn_on' else 'off'
        reply = f"{d.label}已{'打开' if action == 'turn_on' else '关闭'}。"
    elif action.startswith('brightness'):
        modes = set(attrs.get('supported_color_modes') or [])
        if not modes - {'onoff', 'unknown'}:
            return f'{d.label}不支持调节亮度。'
        value = command.value
        if value is None:
            brightness = attrs.get('brightness')
            if current.get('state') != 'on' or not _number(brightness):
                return f'{d.label}还没有可用亮度，请直接指定亮度百分比。'
            value = max(1, min(100, round(brightness / 255 * 100) + (10 if action.endswith('up') else -10)))
        service = 'turn_off' if value == 0 else 'turn_on'
        if value:
            data['brightness_pct'] = value; expected['brightness_pct'] = value
        expected['state'] = 'on' if value else 'off'
        reply = f'{d.label}亮度已调到百分之{value}。' if value else f'{d.label}已关闭。'
    else:
        features = attrs.get('supported_features', 0)
        feature = {'volume': 4, 'volume_up': 4, 'volume_down': 4, 'mute': 8, 'unmute': 8, 'pause': 1, 'play': 16384, 'stop': 16}[action]
        if not isinstance(features, int) or not features & feature:
            return f'{d.label}不支持这项操作。'
        if action.startswith('volume'):
            value = command.value
            if value is None:
                current_volume = attrs.get('volume_level')
                if not _number(current_volume): return f'{d.label}当前音量未知，请直接指定音量百分比。'
                value = max(0, min(100, round(current_volume * 100) + (10 if action.endswith('up') else -10)))
            service = 'volume_set'; data['volume_level'] = value / 100; expected['volume_level'] = value / 100
            reply = f'{d.label}音量已调到百分之{value}。'
        elif action in ('pause', 'play', 'stop'):
            service = {'pause': 'media_pause', 'play': 'media_play', 'stop': 'media_stop'}[action]
            expected['state'] = {'pause': 'paused', 'play': 'playing', 'stop': ('idle', 'paused', 'off')}[action]
            reply = f"{d.label}已" + {'pause': '暂停', 'play': '继续播放', 'stop': '停止播放'}[action] + '。'
        else:
            service = 'volume_mute'; data['is_volume_muted'] = action == 'mute'; expected['is_volume_muted'] = action == 'mute'
            reply = f"{d.label}已{'静音' if action == 'mute' else '取消静音'}。"
    return service, data, expected, reply


def confirmed(state, expected):
    attrs = state.get('attributes') or {}
    for key, value in expected.items():
        if key == 'state':
            if state.get('state') not in (value if isinstance(value, tuple) else (value,)): return False
        elif key == 'brightness_pct':
            if not _number(attrs.get('brightness')) or abs(attrs['brightness'] / 255 * 100 - value) > 2: return False
        elif key == 'volume_level':
            if not _number(attrs.get(key)) or abs(attrs[key] - value) > .015: return False
        elif attrs.get(key) != value:
            return False
    return True


async def execute(command, ha, stale=lambda: False, transport=None):
    """One request at most for a mutation. Never retry an uncertain request via the model."""
    device = command.device
    if device.entity_id not in {d.entity_id for d in devices(ha)}:
        return _outcome('这个设备没有配置为可直接控制。')
    base, token = str(ha.get('base_url') or '').rstrip('/'), ha.get('api_key')
    if not base.startswith(('http://', 'https://')) or not token:
        return _outcome('Home Assistant 连接尚未配置。')
    sent = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2), transport=transport,
                                     follow_redirects=False, trust_env=False,
                                     headers={'Authorization': f'Bearer {token}'}) as client:
            async def read():
                r = await client.get(base + '/api/states/' + device.entity_id)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or data.get('entity_id') != device.entity_id: raise ValueError('Invalid HA state')
                return data
            _fresh(stale)
            current = await read()
            _fresh(stale)
            if command.action == 'state':
                known = current.get('state') not in ('unavailable', 'unknown', None)
                return _outcome(state_reply(device, current), known)
            if current.get('state') in ('unavailable', 'unknown'):
                return _outcome(f'{device.label}暂时离线或状态未知，未执行操作。')
            call = service_call(command, current)
            if isinstance(call, str): return _outcome(call)
            service, payload, expected, reply = call
            _fresh(stale)
            sent = True
            result = await client.post(base + '/api/services/' + device.domain + '/' + service, json=payload)
            result.raise_for_status()
            _fresh(stale)
            # Allow the HA integration up to 0.8 seconds to report the resulting state.
            for delay in (0, .25, .55):
                if delay: await asyncio.sleep(delay)
                _fresh(stale)
                observed = await read()
                if confirmed(observed, expected): return _outcome(reply, True)
            return _outcome(f'已向{device.label}发送指令，设备状态尚未更新。')
    except Superseded:
        raise
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403): return _outcome('Home Assistant 拒绝访问，请检查设备授权。')
        if error.response.status_code == 404: return _outcome(f'{device.label}的设备或服务不存在。')
        return _outcome(f'{device.label}操作结果未确认。' if sent else f'暂时无法读取{device.label}的状态。')
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return _outcome(f'{device.label}操作结果未确认，请稍后查询状态。' if sent else '暂时无法连接 Home Assistant，请稍后重试。')


async def handle(conn, text, gen):
    """Backward-compatible entry; the shared hook owns reply and no-LLM routing."""
    from core.agent.pre_llm import handle as run_hooks
    return await run_hooks(conn, text, gen)
