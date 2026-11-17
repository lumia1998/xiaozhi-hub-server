"""Managed Markdown skills and MCP connections for the Pi models."""
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from config.config_loader import get_project_dir

_lock = threading.RLock()
ENGINES = {'api'}
MAX_FILE = 512 * 1024


def default_path():
    return Path(get_project_dir()) / 'data/llm_extensions.json'


def load(path=None):
    path = Path(path or default_path())
    with _lock:
        if not path.exists():
            return {'skills': [], 'mcp_servers': []}
        raw = path.read_bytes()
        if len(raw) > MAX_FILE:
            raise ValueError('扩展配置文件过大')
        return json.loads(raw)


def revision(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def describe(path=None):
    data = load(path)
    result = copy.deepcopy(data)
    for row in result['mcp_servers']:
        for key in ('headers', 'env'):
            row[key + '_keys'] = sorted(row.get(key, {}))
            row.pop(key, None)
    return {**result, 'revision': revision(data)}


def _text(value, label, limit, required=True):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError(label + '格式或长度不正确')
    value = value.strip()
    if required and not value:
        raise ValueError('请填写' + label)
    return value


def _strings(value, label, limit=128):
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(v, str) and len(v) <= 2000 for v in value):
        raise ValueError(label + '需要是字符串数组')
    return value


def _map(value, label):
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError(label + '需要是 JSON 对象')
    for k, v in value.items():
        if not isinstance(k, str) or not k or not isinstance(v, str) or '\x00' in k + v or len(k + v) > 8192:
            raise ValueError(label + '的名称和值必须是文本')
        if label == '请求头' and ('\n' in k + v or '\r' in k + v):
            raise ValueError('请求头不能包含换行')
    return dict(value)


def normalize_row(row, kind, old=None):
    if not isinstance(row, dict):
        raise ValueError('扩展条目需要是对象')
    rid = _text(row.get('id'), '标识', 64)
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', rid):
        raise ValueError('标识只能包含字母、数字、下划线和横线')
    engines = _strings(row.get('engines', ['api']), '适用引擎', 8)
    if not engines or not set(engines) <= ENGINES:
        raise ValueError('请选择适用引擎')
    if not isinstance(row.get('enabled', True), bool):
        raise ValueError('启用状态需要是布尔值')
    result = {'id': rid, 'name': _text(row.get('name'), '名称', 80),
              'enabled': row.get('enabled', True), 'engines': list(dict.fromkeys(engines))}
    if kind == 'skills':
        result['description'] = _text(row.get('description', ''), '用途', 500, False)
        result['content'] = _text(row.get('content'), 'Skill 内容', 24000)
        return result
    transport = row.get('transport', 'streamable-http')
    if transport not in ('stdio', 'sse', 'streamable-http'):
        raise ValueError('不支持这个 MCP 传输方式')
    result['transport'] = transport
    try: timeout = float(row.get('timeout', 15))
    except (TypeError, ValueError): raise ValueError('超时需要是 1 到 60 秒')
    if not 1 <= timeout <= 60: raise ValueError('超时需要是 1 到 60 秒')
    result['timeout'] = timeout
    if transport == 'stdio':
        result['command'] = _text(row.get('command'), '程序', 1000)
        result['args'] = _strings(row.get('args', []), '启动参数')
        key = 'env'
    else:
        url = _text(row.get('url'), '服务地址', 2000)
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('请使用 HTTP(S) 地址，认证信息填写在请求头中')
        result['url'] = url
        key = 'headers'
    value = row.get(key)
    result[key] = _map((old or {}).get(key, {}) if value is None else value, '请求头' if key == 'headers' else '环境变量')
    return result


def save(payload, path=None):
    path = Path(path or default_path())
    with _lock:
        current = load(path)
        if payload.get('revision') != revision(current):
            raise ValueError('配置已被修改，请刷新后重试')
        result = {}
        for kind in ('skills', 'mcp_servers'):
            rows = payload.get(kind)
            if not isinstance(rows, list) or len(rows) > 32:
                raise ValueError('每种扩展最多 32 项')
            old = {r['id']: r for r in current[kind]}
            result[kind] = [normalize_row(r, kind, old.get(r.get('id'))) for r in rows if isinstance(r, dict)]
            if len(result[kind]) != len(rows) or len({r['id'] for r in result[kind]}) != len(rows):
                raise ValueError('条目格式错误或标识重复')
        if sum(len(s['content']) for s in result['skills'] if s['enabled']) > 48000:
            raise ValueError('启用的 Skill 内容合计不能超过 48000 字符')
        raw = json.dumps(result, ensure_ascii=False, indent=2)
        if len(raw.encode()) > MAX_FILE: raise ValueError('扩展配置过大')
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.extensions-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        return describe(path)


def add_skills(dialogue, engine, path=None):
    rows = [s for s in load(path)['skills'] if s['enabled'] and engine in s['engines']]
    if not rows: return dialogue
    instructions = '\n\n'.join('### ' + s['name'] + '\n' + s['description'] + '\n' + s['content'] for s in rows)
    result = [dict(m) for m in dialogue]
    extra = '\n\n<configured_skills>\n以下是管理员配置的技能说明，按用途应用。工具执行以实际可用工具为准。\n' + instructions + '\n</configured_skills>'
    if result and result[0]['role'] == 'system': result[0]['content'] = (result[0].get('content') or '') + extra
    else: result.insert(0, {'role': 'system', 'content': extra.strip()})
    return result


def mcp_configs(engine='api', path=None):
    path = Path(path or default_path())
    if not path.exists():
        legacy = path.parent / '.mcp_server_settings.json'
        return json.loads(legacy.read_text()).get('mcpServers', {}) if legacy.exists() and engine == 'api' else {}
    rows = load(path)['mcp_servers']
    return {r['id']: {k: v for k, v in r.items() if k not in ('id', 'name', 'enabled', 'engines')}
            for r in rows if r['enabled'] and engine in r['engines']}


async def probe(row, path=None):
    from core.providers.tools.server_mcp.mcp_client import ServerMCPClient
    import asyncio
    old = next((r for r in load(path)['mcp_servers'] if r['id'] == row.get('id')), None)
    config = normalize_row(row, 'mcp_servers', old)
    client = ServerMCPClient(config)
    try:
        await asyncio.wait_for(client.initialize(), timeout=config['timeout'])
        return {'ok': True, 'tools': [{'name': t.name, 'description': t.description or ''} for t in client.tools]}
    except Exception:
        return {'ok': False, 'error': '连接失败，请检查地址、认证、启动程序和超时设置'}
    finally:
        await client.cleanup()
