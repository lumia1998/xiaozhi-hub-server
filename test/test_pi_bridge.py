"""Pi 引擎桥：模型服务的报错要原样（去掉密钥）传出来，找得到 Node，坏输出和进程退出不卡住请求。"""
import http.server
import json
import queue
import subprocess
import threading
from pathlib import Path

import pytest

from core.providers.llm.pi import pi

KEY = 'sk-' + 'test' * 5
WRONG_KEY = 'sk-' + 'wrong' * 4


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(msg)

    warning = info


class _Process:
    def __init__(self, stdout=(), stderr=()):
        self.stdout = iter(stdout)
        self.stderr = iter(stderr)

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def log(monkeypatch):
    log = _Log()
    monkeypatch.setattr(pi, 'logger', log)
    return log


@pytest.fixture
def fresh_node_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(pi, 'RUNTIME', tmp_path)
    pi.node_binary.cache_clear()
    yield tmp_path
    pi.node_binary.cache_clear()


def test_redact_hides_keys_and_tokens():
    google_key = 'AIza' + 'q' * 12
    text = pi.redact(f'401 Bearer {KEY} url=https://x/v1beta/models?key={google_key}&alt=sse, invalid sk-abcdefghij')
    assert text == '401 Bearer *** url=https://x/v1beta/models?key=***&alt=sse, invalid sk-***'
    assert len(pi.redact('x' * 2000)) == 500


def test_node_binary_prefers_the_node_bundled_with_the_runtime(fresh_node_lookup, monkeypatch):
    bundled = fresh_node_lookup / 'node_modules' / 'node-linux-arm64' / 'bin' / 'node'
    bundled.parent.mkdir(parents=True)
    bundled.write_text('')
    monkeypatch.setattr(pi.sys, 'platform', 'linux')
    monkeypatch.setattr(pi.platform, 'machine', lambda: 'aarch64')
    monkeypatch.setattr(pi.shutil, 'which', lambda _name: pytest.fail('不该再找系统 Node'))
    assert pi.node_binary() == str(bundled)


@pytest.mark.parametrize('version, ok', [('v22.19.0', True), ('v24.1.0', True), ('v22.18.1', False), ('v20.11.0', False)])
def test_node_binary_falls_back_to_a_system_node_22_19_or_newer(fresh_node_lookup, monkeypatch, version, ok):
    monkeypatch.setattr(pi.shutil, 'which', lambda _name: '/usr/bin/node')
    monkeypatch.setattr(pi.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=version + '\n'))
    if ok:
        assert pi.node_binary() == '/usr/bin/node'
    else:
        with pytest.raises(RuntimeError, match='npm ci'):
            pi.node_binary()


def test_node_binary_without_any_node_says_how_to_fix_it(fresh_node_lookup, monkeypatch):
    monkeypatch.setattr(pi.shutil, 'which', lambda _name: None)
    with pytest.raises(RuntimeError, match='npm ci'):
        pi.node_binary()


def test_read_skips_unparsable_lines_and_fails_the_requests_of_an_exited_process(log):
    bridge = pi.Bridge()
    process, newer = _Process(stdout=['not json\n', '[1, 2]\n', '{"type":"ready","version":"1.5.2"}\n',
                                      '{"id":"r1","type":"text","text":"你好"}\n']), _Process()
    mine, other = queue.Queue(), queue.Queue()
    bridge.requests.update(r1=(process, mine), r2=(newer, other))
    bridge.process = process
    bridge._read(process)
    assert [mine.get_nowait() for _ in range(mine.qsize())] == [
        {'id': 'r1', 'type': 'text', 'text': '你好'},
        {'type': 'error', 'message': 'Pi 引擎进程已退出'},
        {'type': 'done'},
    ]
    assert other.empty() and bridge.process is None
    assert sum('无法解析' in line for line in log.lines) == 2
    assert 'Pi runtime ready: 1.5.2' in log.lines and log.lines[-1] == 'Pi 引擎进程已退出: code=0'


def test_node_stderr_is_logged_redacted(log):
    pi.Bridge()._log_stderr(_Process(stderr=['\n', f'TypeError: boom Bearer {KEY}\n']))
    assert log.lines == ['Pi 引擎: TypeError: boom Bearer ***']


@pytest.fixture
def model_service():
    """本地假的 OpenAI 兼容服务：密钥不对就回 401，并把密钥原样写进报错（有些服务就是这样）。"""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('content-length') or 0))
            if self.headers.get('authorization') != f'Bearer {KEY}':
                body = json.dumps({'error': {'message': f'Incorrect API key provided: {self.headers.get("authorization")}'}})
                self.send_response(401)
                self.send_header('content-type', 'application/json')
                self.end_headers()
                self.wfile.write(body.encode())
                return
            self.send_response(200)
            self.send_header('content-type', 'text/event-stream')
            self.end_headers()
            for chunk in ({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': '你好'}}]},
                          {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}):
                self.wfile.write(b'data: ' + json.dumps(chunk).encode() + b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}/v1'
    server.shutdown()


@pytest.fixture
def runtime(monkeypatch):
    local_runtime = Path(__file__).resolve().parent / '.local' / 'pi-runtime'
    if local_runtime.is_dir():
        monkeypatch.setattr(pi, 'RUNTIME', local_runtime)
        pi.node_binary.cache_clear()
    if not (pi.RUNTIME / 'node_modules' / '@earendil-works' / 'pi-agent-core').is_dir():
        pytest.skip(f'Pi 运行时没装：在 {pi.RUNTIME} 运行 npm ci')
    try:
        pi.node_binary()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    yield
    pi.BRIDGE.close()
    pi.node_binary.cache_clear()


@pytest.mark.parametrize('api_key, expected', [(KEY, '你好'), (WRONG_KEY, '401')])
def test_model_service_answers_and_errors_come_through_the_real_bridge(runtime, model_service, api_key, expected):
    provider = pi.LLMProvider({'model_name': 'deepseek-test', 'base_url': model_service, 'api_key': api_key, 'protocol': 'chat'})
    dialogue = [{'role': 'system', 'content': '你是测试'}, {'role': 'user', 'content': '你好'}]
    try:
        text = ''.join(provider.response('s1', dialogue))
    except RuntimeError as exc:
        text = str(exc)
    assert expected in text
    assert WRONG_KEY not in text
