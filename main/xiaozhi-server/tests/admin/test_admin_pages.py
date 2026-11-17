"""管理后台页面接口：大模型、语音合成、意图识别的读写，切换失败回滚，表情单发。全部使用临时配置文件。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin import catalog, config_store, connections, pages
from core.admin import admin_handler as ah
from core.agent import llm_router
from core.providers.asr import doubao_stream

CUSTOM_YAML = """\
selected_module:
  LLM: ClaudeAPI
  Intent: nointent
LLM:
  ClaudeAPI:
    type: openai
    base_url: https://api.anthropic.com/v1/
    model_name: claude-haiku-5-5
    api_key: 你的Claude API Key
"""


@pytest.fixture
def cfg_path(tmp_path) -> Path:
    p = tmp_path / ".config.yaml"
    p.write_text(CUSTOM_YAML, encoding="utf-8")
    return p


def merged(path):
    return config_store.load_merged(custom_path=path)


# ---------- 大模型 ----------

KEY = "sk-" + "test" * 5


def _configure(path, entry, **fields):
    with config_store.edit_custom(path) as data:
        data["LLM"].setdefault(entry, {}).update(fields)


def _configure_deepseek(path):
    _configure(path, "DeepSeekLLM", model_name="deepseek-chat", api_key=KEY)


def _migrate_to_pi(path, entry, **fields):
    """ops 的迁移 0001 改过的厂商条目：type 从 openai 改成 pi，加 protocol: chat。"""
    _configure(path, entry, type="pi", protocol="chat", api_key=KEY, **fields)


def test_model_choices(cfg_path):
    """能切过去的模型：填好了的厂商条目按 LLM_VENDORS 的顺序在前，模型服务的模型在后。"""
    assert pages.model_choices(merged(cfg_path)) == []  # ClaudeAPI 还是占位 Key
    _configure_deepseek(cfg_path)  # 上游条目：type openai，接口地址在 url 字段
    _migrate_to_pi(cfg_path, "KimiLLM", base_url="https://api.moonshot.cn/v1", model_name="kimi-latest")
    with config_store.edit_custom(cfg_path) as data:
        data["model_services"] = {"lan": {"name": "局域网", "base_url": "http://192.168.1.5:1234/v1", "api_key": ""}}
        data["LLM"]["Service_lan_qwen"] = {"type": "pi", "service_id": "lan", "model_name": "qwen3-8b"}
    choices = pages.model_choices(merged(cfg_path))
    assert choices == [
        {"entry": "DeepSeekLLM", "vendor": "deepseek", "label": "DeepSeek", "model": "deepseek-chat"},
        {"entry": "KimiLLM", "vendor": "kimi", "label": "Kimi", "model": "kimi-latest"},  # type: pi 的厂商条目只列一次
        {"entry": "Service_lan_qwen", "vendor": "custom", "label": "qwen3-8b", "model": "qwen3-8b"},
    ]
    assert KEY not in str(choices)  # /kiosk/llm 不用登录


# ---------- 语音识别、语音合成、意图识别 ----------

def test_describe_asr_and_tts(cfg_path):
    asr = pages.describe_asr(merged(cfg_path))
    assert asr["selected"] and "label" in asr
    tts = pages.describe_tts(merged(cfg_path))
    assert tts["voice"] == "zh-CN-XiaoxiaoNeural" and len(tts["voices"]) == 14
    assert all(v["value"].startswith("zh-") for v in tts["voices"])


def test_save_tts_voice(cfg_path):
    with pytest.raises(ValueError):
        pages.save_tts_voice("en-US-AvaNeural", merged(cfg_path), cfg_path)
    assert pages.save_tts_voice("zh-CN-YunxiNeural", merged(cfg_path), cfg_path) == ["voice"]
    assert merged(cfg_path)["TTS"]["EdgeTTS"]["voice"] == "zh-CN-YunxiNeural"


def test_save_vad_silence(cfg_path):
    v = pages.describe_asr(merged(cfg_path))["vad"]
    assert v["selected"] == "SileroVAD" and v["supported"] and v["silence_ms"] == 800
    for bad in ("abc", "100", "6000", "1.5"):
        with pytest.raises(ValueError, match="200～5000"):
            pages.save_vad_silence(bad, merged(cfg_path), cfg_path)
    assert pages.save_vad_silence("1000", merged(cfg_path), cfg_path) == 1000
    assert merged(cfg_path)["VAD"]["SileroVAD"]["min_silence_duration_ms"] == 1000
    assert pages.save_vad_silence(1000, merged(cfg_path), cfg_path) is None  # 没变不写


def test_describe_vad_flags_stream_asr(cfg_path):
    cfg_path.write_text(CUSTOM_YAML.replace("selected_module:\n", "selected_module:\n  ASR: AliyunStreamASR\n"),
                        encoding="utf-8")
    assert pages.describe_vad(merged(cfg_path))["asr_stream"] is True
    # 豆包也是流式，但它按这里的时长断句
    cfg_path.write_text(CUSTOM_YAML.replace("selected_module:\n", "selected_module:\n  ASR: DoubaoStreamASRV2\n"),
                        encoding="utf-8")
    v = pages.describe_vad(merged(cfg_path))
    assert v["asr_stream"] is False and v["asr_doubao"] is True


def test_intent_plugins_keep_manual_extras(cfg_path):
    cfg_path.write_text(CUSTOM_YAML + """Intent:
  function_call:
    functions: [get_weather, my_custom_plugin]
""", encoding="utf-8")
    d = pages.describe_intent(merged(cfg_path))
    assert d["mode"] == "nointent" and d["extra"] == ["my_custom_plugin"]
    assert next(p for p in d["plugins"] if p["name"] == "web_search")["needs_config"] is True  # 上游默认 Key 是 mk-xxx
    with pytest.raises(ValueError):
        pages.save_intent_plugins(["evil"], merged(cfg_path), cfg_path)
    pages.save_intent_plugins(["play_music", "get_weather"], merged(cfg_path), cfg_path)
    assert merged(cfg_path)["Intent"]["function_call"]["functions"] == ["get_weather", "play_music", "my_custom_plugin"]


# ---------- HTTP：切换、回滚、联动 ----------

@pytest.fixture
async def client(cfg_path):
    app = web.Application()
    config = {"selected_module": {"LLM": "ClaudeAPI", "Intent": "nointent"}, "LLM": {}, "Intent": {}}
    handler = ah.AdminHandler(config, ws_server=None, config_path=cfg_path)
    handler.register(app)
    async with TestClient(TestServer(app)) as c:
        await c.post("/admin/api/setup", json={"password": "pass-1234"})
        c.handler = handler
        yield c


async def test_switch_failure_restores_selection(cfg_path, monkeypatch):
    """切换失败时，磁盘上的选择要恢复成原来的，避免下次启动出问题。"""
    def boom(*a, **k):
        raise RuntimeError("load failed.")
    monkeypatch.setattr(ah, "initialize_modules", boom)
    ws = SimpleNamespace(config_lock=asyncio.Lock(), _llm="old-llm")
    running = {"selected_module": {"LLM": "ClaudeAPI"}, "LLM": {}}
    h = ah.AdminHandler(running, ws_server=ws, config_path=cfg_path)
    result = await h._switch("LLM", "ChatGLMLLM")
    assert result["ok"] is False and "已恢复为原来的 ClaudeAPI" in result["note"] and "。。" not in result["note"]
    assert merged(cfg_path)["selected_module"]["LLM"] == "ClaudeAPI" and ws._llm == "old-llm"


async def test_page_endpoints(client):
    assert (await client.get("/admin/api/asr")).status == 200
    tts = await (await client.get("/admin/api/tts")).json()
    assert len(tts["voices"]) == 14
    assert (await client.get("/admin/api/tts/preview?voice=en-US-AvaNeural")).status == 400
    r = await client.post("/admin/api/tts", json={"voice": "zh-CN-YunxiNeural"})
    assert r.status == 200 and (await r.json())["changed"] == ["voice"]
    intent = await (await client.get("/admin/api/intent")).json()
    assert intent["mode"] == "nointent"
    for path in ("model-services", "asr", "tts", "intent"):
        await client.post("/admin/api/logout", json={})
        assert (await client.get(f"/admin/api/{path}")).status == 401
        await client.post("/admin/api/login", json={"password": "pass-1234"})


# ---------- 本机设备接口（/kiosk） ----------

@pytest.fixture
def kiosk(client, tmp_path, monkeypatch):
    """不登录也能用；设备记住的模型写到临时文件。"""
    from core.agent.session_store import SessionStore

    monkeypatch.setattr(llm_router, "_store", SessionStore(tmp_path / "agent_sessions.json", "last_model"))
    return client


async def test_kiosk_llm_list_and_device_model(kiosk, cfg_path):
    _configure_deepseek(cfg_path)
    await kiosk.post("/admin/api/logout", json={})
    data = await (await kiosk.get("/kiosk/llm")).json()
    assert data["ok"] and data["selected"] == data["default"] == "ClaudeAPI"
    assert [(o["vendor"], o["entry"], o["model"]) for o in data["options"]] == \
        [("deepseek", "DeepSeekLLM", "deepseek-chat")]  # 没配好的厂商不列
    # 设备上次切到的模型（呼名或在屏幕上切换）才是它实际会用的
    llm_router.store().put("dev-1", {"model": "DeepSeekLLM"})
    data = await (await kiosk.get("/kiosk/llm?device=dev-1")).json()
    assert data["selected"] == "DeepSeekLLM" and data["default"] == "ClaudeAPI"
    llm_router.store().put("dev-1", {"model": "ChatGLMLLM"})  # 没配好：退回默认
    assert (await (await kiosk.get("/kiosk/llm?device=dev-1")).json())["selected"] == "ClaudeAPI"


async def test_kiosk_llm_use_is_per_device(kiosk, cfg_path, monkeypatch):
    """设备上切模型同呼名切换：只切这台设备（在线连接马上跟着切）、记住它；后台默认模型不变，
    运行中的默认实例不重建；直接返回切换后的模型列表。"""
    switched = []
    monkeypatch.setattr(llm_router, "switch", lambda conn, name, remember=True: switched.append((conn.session_id, name, remember)))
    rebuilt = []
    monkeypatch.setattr(ah, "initialize_modules", lambda *a, **k: rebuilt.append(k) or {})
    _configure_deepseek(cfg_path)
    await kiosk.post("/admin/api/logout", json={})
    before = cfg_path.read_text(encoding="utf-8")
    mine = SimpleNamespace(session_id="s-8", device_id="dev-8", client_ip="127.0.0.1")
    other = SimpleNamespace(session_id="s-9", device_id="dev-9", client_ip="127.0.0.1")
    connections.register(mine)
    connections.register(other)
    try:
        r = await kiosk.post("/kiosk/llm/use", json={"vendor": "deepseek", "device": "dev-8"})
        data = await r.json()
        assert r.status == 200 and data["ok"]
        assert data["selected"] == "DeepSeekLLM" and data["default"] == "ClaudeAPI"  # 返回切换后的列表
        assert [o["entry"] for o in data["options"]] == ["DeepSeekLLM"]
        assert switched == [("s-8", "DeepSeekLLM", False)]  # 别的设备不动
        assert llm_router.store().get("dev-8")["model"] == "DeepSeekLLM"
        assert cfg_path.read_text(encoding="utf-8") == before and rebuilt == []  # 不写配置、不重建默认实例
        # 后台保存了但没设成默认的条目补进运行中的配置，切换时照它创建实例
        assert kiosk.handler.config["LLM"]["DeepSeekLLM"]["model_name"] == "deepseek-chat"
        assert kiosk.handler.config["selected_module"] == {"LLM": "ClaudeAPI", "Intent": "nointent"}

        r = await kiosk.post("/kiosk/llm/use", json={"vendor": "openai", "device": "dev-8"})
        assert r.status == 400 and "还没配置好" in (await r.json())["error"] and len(switched) == 1
        assert (await kiosk.post("/kiosk/llm/use", json={"vendor": "nope", "device": "dev-8"})).status == 404
        assert (await kiosk.post("/kiosk/llm/use", json={"vendor": "deepseek"})).status == 400  # 要说明是哪台设备
    finally:
        connections.unregister(mine)
        connections.unregister(other)


async def test_kiosk_llm_vendor_entry_migrated_to_pi(kiosk, cfg_path):
    """迁移 0001 改成 type: pi 的厂商条目：设备上还按厂商列（设备靠 vendor 选图标、显示厂商名），
    不当成模型服务的模型；只发 vendor 也能切过去。"""
    _migrate_to_pi(cfg_path, "DeepSeekLLM", model_name="deepseek-chat")
    await kiosk.post("/admin/api/logout", json={})
    data = await (await kiosk.get("/kiosk/llm")).json()
    assert data["options"] == [{"kind": "api", "vendor": "deepseek", "label": "DeepSeek", "entry": "DeepSeekLLM",
                                "model": "deepseek-chat"}]
    r = await kiosk.post("/kiosk/llm/use", json={"vendor": "deepseek", "device": "dev-1"})
    assert r.status == 200 and llm_router.store().get("dev-1")["model"] == "DeepSeekLLM"


async def test_kiosk_llm_use_failure_keeps_old_model(kiosk, cfg_path, monkeypatch):
    def boom(conn, name, remember=True):
        raise RuntimeError("Pi 引擎启动失败")
    monkeypatch.setattr(llm_router, "switch", boom)
    _configure_deepseek(cfg_path)
    await kiosk.post("/admin/api/logout", json={})
    llm_router.store().put("dev-8", {"model": "KimiLLM"})
    fake = SimpleNamespace(session_id="s-8", device_id="dev-8", client_ip="127.0.0.1")
    connections.register(fake)
    try:
        r = await kiosk.post("/kiosk/llm/use", json={"vendor": "deepseek", "device": "dev-8"})
        assert r.status == 500 and "Pi 引擎启动失败" in (await r.json())["error"]
        assert llm_router.store().get("dev-8")["model"] == "KimiLLM"  # 还记着原来的
        llm_router.store().forget("dev-8")
        r = await kiosk.post("/kiosk/llm/use", json={"vendor": "deepseek", "device": "dev-8"})
        assert r.status == 500 and llm_router.store().get("dev-8") is None
    finally:
        connections.unregister(fake)


async def test_kiosk_model_service_model_is_per_device(kiosk, cfg_path, monkeypatch):
    """模型服务里的模型也列出来；选它和喊称呼一样：只切这台设备、记住它，默认模型不变。"""
    switched = []
    monkeypatch.setattr(llm_router, "switch", lambda conn, name, remember=True: switched.append((conn.session_id, name, remember)))
    with config_store.edit_custom(cfg_path) as data:
        data["model_services"] = {"lan": {"name": "局域网", "base_url": "http://192.168.1.5:1234/v1", "api_key": ""}}
        data["LLM"]["Service_lan_qwen"] = {"type": "pi", "service_id": "lan", "model_name": "qwen3-8b"}
        data["LLM"]["Service_gone_qwen"] = {"type": "pi", "service_id": "gone", "model_name": "qwen3-8b"}
    await kiosk.post("/admin/api/logout", json={})
    data = await (await kiosk.get("/kiosk/llm?device=dev-1")).json()
    assert data["options"] == [{"kind": "api", "vendor": "custom", "label": "qwen3-8b", "entry": "Service_lan_qwen",
                                "model": "qwen3-8b"}]  # 服务删掉了的不列
    mine = SimpleNamespace(session_id="s-1", device_id="dev-1", client_ip="127.0.0.1")
    other = SimpleNamespace(session_id="s-2", device_id="dev-2", client_ip="127.0.0.1")
    connections.register(mine)
    connections.register(other)
    try:
        body = {"entry": "Service_lan_qwen", "device": "dev-1"}
        r = await kiosk.post("/kiosk/llm/use", json=body)
        assert r.status == 200 and (await r.json())["ok"]
        assert switched == [("s-1", "Service_lan_qwen", False)]  # 别的设备不动
        assert merged(cfg_path)["selected_module"]["LLM"] == "ClaudeAPI"  # 默认模型不变
        assert llm_router.store().get("dev-1")["model"] == "Service_lan_qwen"
        data = await (await kiosk.get("/kiosk/llm?device=dev-1")).json()
        assert data["selected"] == "Service_lan_qwen" and data["default"] == "ClaudeAPI"
        assert (await kiosk.post("/kiosk/llm/use", json={**body, "device": ""})).status == 400
        assert (await kiosk.post("/kiosk/llm/use", json={**body, "entry": "Service_gone_qwen"})).status == 400
        r = await kiosk.post("/kiosk/llm/use", json={**body, "entry": "DeepSeekLLM"})  # 还是占位 Key
        assert r.status == 400 and "DeepSeek 还没配置好" in (await r.json())["error"]
    finally:
        connections.unregister(mine)
        connections.unregister(other)


async def test_kiosk_asr(kiosk):
    pcm = b"\x00\x10" * 16000  # 1 秒
    assert (await kiosk.post("/kiosk/asr", data=pcm)).status == 501  # 服务没在跑识别
    heard = []

    class LocalASR:
        def recognize_pcm(self, data):
            heard.append(len(data))
            return "小麦小麦"
    kiosk.handler.ws_server = SimpleNamespace(_asr=LocalASR())
    data = await (await kiosk.post("/kiosk/asr", data=pcm)).json()
    assert data["ok"] and data["text"] == "小麦小麦" and isinstance(data["ms"], int) and heard == [32000]
    assert (await kiosk.post("/kiosk/asr", data=b"")).status == 400
    assert (await kiosk.post("/kiosk/asr", data=b"\x00" * (ah.KIOSK_ASR_MAX_BYTES + 2))).status == 400
    kiosk.handler.ws_server = SimpleNamespace(_asr=object())  # 豆包之类的远程识别
    assert (await kiosk.post("/kiosk/asr", data=pcm)).status == 501


async def test_kiosk_chat_history_and_clear(kiosk, tmp_path, monkeypatch):
    from core.agent import chat_history

    monkeypatch.setattr(chat_history, "get_project_dir", lambda: str(tmp_path))
    store = chat_history.db()
    store.insert("dev-1", "DeepSeekLLM", "user", "我叫小明")
    deepseek_last = store.insert("dev-1", "DeepSeekLLM", "assistant", "你好小明")
    kimi_last = store.insert("dev-1", "KimiLLM", "user", "讲个笑话")
    store.insert("dev-2", "DeepSeekLLM", "user", "别人的")
    assert (await kiosk.get("/kiosk/chat/history")).status == 400
    data = await (await kiosk.get("/kiosk/chat/history?device=dev-1&limit=1")).json()
    assert data["ok"] and {k: [m["text"] for m in v] for k, v in data["models"].items()} == \
        {"DeepSeekLLM": ["你好小明"], "KimiLLM": ["讲个笑话"]}
    assert (await kiosk.get("/kiosk/chat/history?device=dev-1&limit=x")).status == 400

    store.set_memory("dev-1", "- 用户叫小明", "summary", progress={"DeepSeekLLM": 1})
    data = await (await kiosk.post("/kiosk/chat/clear", json={"device": "dev-1", "memory_only": True})).json()
    assert data == {"ok": True, "deleted": 0}
    # 只清记忆、记录留着；清空前的记录不再总结回记忆（进度跳到各模型的最后一条）
    assert store.memory("dev-1")["summary"] == "" and \
        store.progress("dev-1") == {"DeepSeekLLM": deepseek_last, "KimiLLM": kimi_last}
    assert (await kiosk.post("/kiosk/chat/clear", json={})).status == 400
    data = await (await kiosk.post("/kiosk/chat/clear", json={"device": "dev-1"})).json()
    assert data == {"ok": True, "deleted": 3}
    assert (await (await kiosk.get("/kiosk/chat/history?device=dev-1")).json())["models"] == {}
    assert store.models("dev-2") == ["DeepSeekLLM"]


async def test_memory_page(client, tmp_path):
    from core.agent import chat_history

    store = chat_history.db()
    store.insert("dev-1", "DeepSeekLLM", "user", "我叫小明")
    store.insert("dev-1", "KimiLLM", "user", "讲个笑话")
    store.set_memory("dev-1", "- 用户叫小明", "summary")
    data = await (await client.get("/admin/api/memory")).json()
    assert data["ok"] and data["settings"]["max_messages"] == 30
    [dev] = data["devices"]
    assert dev["device"] == "dev-1" and dev["memory"]["summary"] == "- 用户叫小明" and dev["memory"]["source"] == "summary"
    assert dev["memory"]["updated"] and dev["online"] is False
    assert {(m["entry"], m["count"]) for m in dev["models"]} == {("DeepSeekLLM", 1), ("KimiLLM", 1)}

    v, auto = dev["memory"]["version"], dev["memory"]["summary"]
    # 后台分两栏改记忆；不指定 kind 时改手动保留的那份，自动整理的不动
    r = await client.post("/admin/api/memory/save", json={"device": "dev-1", "summary": "- 用户叫小红", "version": v})
    mem = (await r.json())["memory"]
    assert r.status == 200 and mem["pinned"] == "- 用户叫小红" and mem["summary"] == auto
    assert store.memory("dev-1")["source"] == "manual"
    r = await client.post("/admin/api/memory/save", json={"device": "dev-1", "summary": "旧的", "version": v})
    assert r.status == 409 and (await r.json())["memory"]["pinned"] == "- 用户叫小红"
    r = await client.post("/admin/api/memory/save", json={"device": "dev-1", "kind": "summary", "summary": ""})
    mem = store.memory("dev-1")
    assert r.status == 200 and mem["summary"] == "" and mem["pinned"] == "- 用户叫小红"
    assert (await client.post("/admin/api/memory/save", json={"device": "dev-1"})).status == 400
    assert (await client.post("/admin/api/memory/save", json={"device": "dev-1", "kind": "x", "summary": "x"})).status == 400
    assert (await client.post("/admin/api/memory/save", json={"device": "dev-1", "summary": "长" * 5001})).status == 400

    await client.post("/admin/api/logout", json={})
    assert (await client.get("/admin/api/memory")).status == 401
    assert (await client.post("/admin/api/memory/save", json={"device": "dev-1", "summary": "x"})).status == 401


def test_sherpa_recognize_pcm():
    from core.providers.asr.sherpa_onnx_local import ASRProvider

    seen = {}

    class Stream:
        result = SimpleNamespace(text="小麦小麦")

        def accept_waveform(self, rate, samples):
            seen.update(rate=rate, samples=samples)

    asr = ASRProvider.__new__(ASRProvider)
    asr.model = SimpleNamespace(create_stream=Stream, decode_stream=lambda s: None)
    assert asr.recognize_pcm(b"\x00\x40\x00\xc0\x01") == "小麦小麦"  # 多出的半个采样丢掉
    assert seen["rate"] == 16000 and list(seen["samples"]) == [0.5, -0.5]


async def test_kiosk_only_from_this_machine(kiosk, monkeypatch):
    calls = [("get", "/kiosk/llm", None), ("post", "/kiosk/llm/use", {"vendor": "deepseek", "device": "dev-1"}),
             ("post", "/kiosk/asr", None), ("post", "/kiosk/tts", {"text": "该晾衣服了"}),
             ("get", "/kiosk/chat/history?device=dev-1", None), ("post", "/kiosk/chat/clear", {"device": "dev-1"})]
    for method, path, body in calls:  # 本机反向代理转发来的不算本机
        r = await getattr(kiosk, method)(path, json=body, headers={"X-Forwarded-For": "203.0.113.5"})
        assert r.status == 403
    monkeypatch.setattr(web.BaseRequest, "remote", property(lambda self: "192.168.1.20"))  # 局域网里的其他电脑
    for method, path, body in calls:
        r = await getattr(kiosk, method)(path, json=body)
        assert r.status == 403 and (await r.json())["error"] == "仅限本机"


async def test_emotion_single_and_stop(client):
    sent = []

    class FakeWS:
        async def send(self, data):
            sent.append(data)

    fake = SimpleNamespace(session_id="s-9", device_id="dev-9", client_ip="x", websocket=FakeWS())
    connections.register(fake)
    try:
        r = await client.post("/admin/api/devices/s-9/emotion", json={"emotion": "happy"})
        assert r.status == 200 and '"emotion": "happy"' in sent[-1]
        assert (await client.post("/admin/api/devices/s-9/emotion", json={"emotion": "nope"})).status == 400
        assert (await client.post("/admin/api/devices/none/emotion", json={"emotion": "happy"})).status == 404
        await client.post("/admin/api/devices/s-9/emotion-test", json={"interval": 1})
        r = await client.post("/admin/api/devices/s-9/emotion-test/stop", json={})
        assert r.status == 200
        for _ in range(50):
            await asyncio.sleep(0)
        devices = (await (await client.get("/admin/api/devices")).json())
        progress = devices["devices"][0]["emotion_test"]
        assert progress["running"] is False and progress["total"] == 21
        assert len(devices["emotions"]) == 21 and devices["emotions"][0]["label"]
    finally:
        connections.unregister(fake)


# ---------- 呼名 ----------

def test_describe_call_names_defaults(cfg_path):
    d = pages.describe_call_names(merged(cfg_path))
    assert d["enabled"] is True and d["default"] == "ClaudeAPI"
    assert d["models"] == [] and d["targets"] == []  # 还没配称呼；API 厂商都还没填 Key，不能选


def test_describe_call_names_targets(cfg_path):
    with config_store.edit_custom(cfg_path) as data:
        data["call_names"] = {"models": {"DeepSeekLLM": ["deepseek"], "KimiLLM": ["kimi"]}}
    _configure_deepseek(cfg_path)
    d = pages.describe_call_names(merged(cfg_path))
    assert [(m["entry"], m["aliases"], m["ready"]) for m in d["models"]] == \
        [("DeepSeekLLM", ["deepseek"], True), ("KimiLLM", ["kimi"], False)]
    assert {t["entry"]: t["defaults"] for t in d["targets"]} == {"DeepSeekLLM": ["deepseek"]}


def test_call_targets_defaults_use_english_vendor_id(cfg_path):
    for entry in ("AliLLM", "ClaudeAPI"):
        _configure(cfg_path, entry, api_key=KEY)
    _migrate_to_pi(cfg_path, "DeepSeekLLM", model_name="deepseek-chat")
    targets = {t["entry"]: t for t in pages.call_targets(merged(cfg_path))}
    assert targets["AliLLM"]["defaults"] == ["qwen"]  # 中文名“通义千问”也只填英文
    assert targets["ClaudeAPI"]["defaults"] == ["claude"]
    # 迁移 0001 改成 type: pi 的厂商条目也按厂商：显示厂商名、预填厂商的称呼
    assert targets["DeepSeekLLM"] == {"entry": "DeepSeekLLM", "label": "DeepSeek（API）", "defaults": ["deepseek"]}


def test_save_call_names_validates_and_writes(cfg_path):
    _configure_deepseek(cfg_path)
    _configure(cfg_path, "ClaudeAPI", api_key=KEY)
    payload = {"reply": "在呢", "models": [{"entry": "ClaudeAPI", "aliases": ["Cloud", "克劳德", "cloud", " "]},
                                          {"entry": "DeepSeekLLM", "aliases": ["深度求索"]}]}
    changed = pages.save_call_names(payload, merged(cfg_path), cfg_path)
    assert set(changed) == {"models", "reply"}
    d = pages.describe_call_names(merged(cfg_path))
    assert [(m["entry"], m["aliases"]) for m in d["models"]] == [("ClaudeAPI", ["cloud", "克劳德"]),
                                                                ("DeepSeekLLM", ["深度求索"])]  # 去重、去空、转小写
    assert d["reply"] == "在呢"
    assert pages.save_call_names(payload, merged(cfg_path), cfg_path) == []

    def models(*items):
        return {"models": [{"entry": e, "aliases": a} for e, a in items]}

    for bad, msg in ((models(("ClaudeAPI", ["cloud code"])), "空格或标点"), (models(("ClaudeAPI", ["c"])), "2 到 20"),
                     (models(("ClaudeAPI", ["hi"])), "招呼语"), (models(("ClaudeAPI", [])), "至少要有一个称呼"),
                     (models(), "至少要给一个模型"), (models(("KimiLLM", ["kimi"])), "还没配置好"),
                     (models(("ClaudeAPI", ["cloud"]), ("DeepSeekLLM", ["cloud"])), "只能给一个模型"),
                     (models(("ClaudeAPI", ["cloud"]), ("ClaudeAPI", ["claude"])), "重复")):
        with pytest.raises(ValueError, match=msg):
            pages.save_call_names(bad, merged(cfg_path), cfg_path)
    assert pages.save_call_names({"models": [], "enabled": False}, merged(cfg_path), cfg_path)  # 关闭时可以不留称呼


def test_save_call_names_keeps_model_no_longer_configured(cfg_path):
    _configure_deepseek(cfg_path)
    pages.save_call_names({"models": [{"entry": "DeepSeekLLM", "aliases": ["deepseek"]}]}, merged(cfg_path), cfg_path)
    with config_store.edit_custom(cfg_path) as data:
        data["LLM"]["DeepSeekLLM"]["api_key"] = "你的key"  # 之后 Key 被清掉了
    d = pages.describe_call_names(merged(cfg_path))
    assert d["models"][0]["ready"] is False
    # 原来就有的可以保留，只改称呼也能保存
    assert pages.save_call_names({"models": [{"entry": "DeepSeekLLM", "aliases": ["deepseek", "深度"]}]},
                                 merged(cfg_path), cfg_path) == ["models"]


async def test_call_names_endpoints_apply_to_online_connections(client, cfg_path):
    from core.agent.call_names import learner

    _configure_deepseek(cfg_path)
    fake = SimpleNamespace(session_id="s-7", device_id="dev-7", client_ip="x", config={})
    connections.register(fake)
    try:
        d = await (await client.get("/admin/api/call-names")).json()
        assert d["ok"] and d["devices"][0]["session_id"] == "s-7" and d["learning"]["active"] is False
        r = await client.post("/admin/api/call-names", json={"models": [{"entry": "DeepSeekLLM", "aliases": ["deepseek", "深度求索"]}]})
        assert r.status == 200 and (await r.json())["changed"] == ["models"]
        assert fake.config["call_names"]["models"]["DeepSeekLLM"] == ["deepseek", "深度求索"]  # 在线连接马上生效
        assert client.handler.config["call_names"]["models"]["DeepSeekLLM"] == ["deepseek", "深度求索"]
        bad = {"models": [{"entry": "DeepSeekLLM", "aliases": ["x y"]}]}
        assert (await client.post("/admin/api/call-names", json=bad)).status == 400

        r = await (await client.post("/admin/api/call-names/learn", json={"target": "DeepSeekLLM"})).json()
        assert r["learning"]["active"] and r["learning"]["session_id"] == "s-7"  # 只有一台在线时自动选它
        assert r["learning"]["target"] == "DeepSeekLLM"
        assert (await client.post("/admin/api/call-names/learn", json={"session_id": "gone"})).status == 400
        r = await (await client.post("/admin/api/call-names/learn/stop", json={})).json()
        assert r["learning"]["active"] is False
    finally:
        learner.stop()
        connections.unregister(fake)
    assert (await client.post("/admin/api/call-names/learn", json={})).status == 400  # 没有设备在线


async def test_vad_save_applies_to_running_instances(cfg_path):
    shared = SimpleNamespace(silence_threshold_ms=200)
    own = SimpleNamespace(silence_threshold_ms=200)  # 设备有私有配置时，连接用的是自己的实例
    ws = SimpleNamespace(config_lock=asyncio.Lock(), _vad=shared)
    running = {"selected_module": {"VAD": "SileroVAD"}, "VAD": {"SileroVAD": {"min_silence_duration_ms": 200}}}
    app = web.Application()
    handler = ah.AdminHandler(running, ws_server=ws, config_path=cfg_path)
    handler.register(app)
    doubao = SimpleNamespace(interface_type="stream", end_window_size=200)  # 连接自己的豆包实例
    fake = SimpleNamespace(session_id="s-8", device_id="dev-8", client_ip="x", vad=own, asr=doubao,
                           config={"VAD": {"SileroVAD": {"min_silence_duration_ms": 200}}})
    connections.register(fake)
    try:
        async with TestClient(TestServer(app)) as c:
            await c.post("/admin/api/setup", json={"password": "pass-1234"})
            r = await c.post("/admin/api/vad", json={"silence_ms": "6000"})
            assert r.status == 400 and "200～5000" in (await r.json())["error"]
            r = await (await c.post("/admin/api/vad", json={"silence_ms": "900"})).json()
            assert r["note"] == "已保存，马上生效"
            assert shared.silence_threshold_ms == 900 and own.silence_threshold_ms == 900
            assert doubao.end_window_size == 900  # 豆包下一句话就用新的判停时长
            assert running["VAD"]["SileroVAD"]["min_silence_duration_ms"] == 900
            assert fake.config["VAD"]["SileroVAD"]["min_silence_duration_ms"] == 900
            assert merged(cfg_path)["VAD"]["SileroVAD"]["min_silence_duration_ms"] == 900
            r = await (await c.post("/admin/api/vad", json={"silence_ms": 900})).json()
            assert r["note"] == "没有修改"
            await c.post("/admin/api/logout", json={})
            assert (await c.post("/admin/api/vad", json={"silence_ms": 800})).status == 401
    finally:
        connections.unregister(fake)


def test_asr_model_check_by_model_type(tmp_path):
    entry = {"type": "sherpa_onnx_local", "model_type": "funasr_nano", "model_dir": str(tmp_path)}
    assert pages.asr_label(entry) == "本地识别 · Fun-ASR-Nano（sherpa-onnx），不需要联网"
    r = pages.asr_model_check(entry)
    assert r["ok"] is False and "llm.int8.onnx" in r["detail"]
    (tmp_path / "llm.int8.onnx").write_bytes(b"\0" * (101 * 1024 * 1024))
    for name in ("encoder_adaptor.int8.onnx", "embedding.int8.onnx"):
        (tmp_path / name).write_bytes(b"x")
    assert "Qwen3-0.6B" in pages.asr_model_check(entry)["detail"]  # 分词器目录也要有
    (tmp_path / "Qwen3-0.6B").mkdir()
    assert pages.asr_model_check(entry) == {"ok": True, "detail": "llm.int8.onnx 101MB"}
    # 没写 model_type 时按上游默认的 SenseVoice 检查
    assert "model.int8.onnx" in pages.asr_model_check({"type": "sherpa_onnx_local", "model_dir": str(tmp_path)})["detail"]
    assert pages.asr_model_check({"type": "doubao_stream"}) is None


def test_default_xasr_entry_and_label(tmp_path):
    """config.yaml 里本项目默认的 X-ASR 条目：transducer 类型，支持热词（默认热词在代码里），后台显示为 X-ASR。"""
    empty = tmp_path / ".config.yaml"
    empty.write_text("{}\n", encoding="utf-8")
    cfg = config_store.load_merged(custom_path=empty)
    entry = cfg["ASR"]["SherpaXASR"]
    assert entry["type"] == "sherpa_onnx_local" and entry["model_type"] == "transducer"
    assert "hotwords" not in entry and pages.hotwords_supported(entry)
    words = pages.describe_hotwords({**cfg, "selected_module": {"ASR": "SherpaXASR"}})["words"]
    assert "Claude Code" in words and "StackChan" in words
    assert pages.asr_label(entry) == "本地识别 · X-ASR 中英混合（sherpa-onnx），不需要联网"
    tmpl = (Path(__file__).resolve().parents[4] / "scripts" / "config.template.yaml").read_text(encoding="utf-8")
    assert "ASR: SherpaXASR" in tmpl


XASR_YAML = CUSTOM_YAML.replace("selected_module:\n", "selected_module:\n  ASR: SherpaXASR\n")


def test_save_hotwords(cfg_path):
    cfg_path.write_text(XASR_YAML, encoding="utf-8")
    h = pages.describe_asr(merged(cfg_path))["hotwords"]
    assert h["supported"] and "StackChan" in h["words"] and "Claude" not in h["auto"]  # Claude 已在默认热词里
    with pytest.raises(ValueError, match="逗号"):
        pages.save_hotwords(["a,b"], merged(cfg_path), cfg_path)
    assert pages.save_hotwords(["GitHub", " Stack  Chan ", "github", ""], merged(cfg_path), cfg_path) == ["hotwords"]
    assert merged(cfg_path)["asr_hotwords"] == ["GitHub", "Stack Chan"]  # 存在顶层，整个覆盖、去重
    assert pages.save_hotwords(["GitHub", "Stack Chan"], merged(cfg_path), cfg_path) == []
    # SenseVoice 不支持热词
    cfg_path.write_text(CUSTOM_YAML.replace("selected_module:\n", "selected_module:\n  ASR: SherpaASR\n"), encoding="utf-8")
    assert pages.describe_hotwords(merged(cfg_path))["supported"] is False
    with pytest.raises(ValueError, match="不支持热词"):
        pages.save_hotwords(["x"], merged(cfg_path), cfg_path)


def test_save_corrections(cfg_path):
    d = pages.describe_corrections(merged(cfg_path))
    assert d[0]["target"] == "Claude Code" and "cloud code" in d[0]["aliases"]
    for bad, msg in [([{"target": "", "aliases": ["xx"]}], "正确写法"), ([{"target": "A", "aliases": ["x"]}], "2～40"),
                     ([{"target": "A", "aliases": ["xy"]}, {"target": "B", "aliases": ["XY"]}], "只能保留一个"),
                     ([{"target": "A", "aliases": ["a"]}], "2～40"), ([{"target": "Abc", "aliases": ["abc"]}], "还没有")]:
        with pytest.raises(ValueError, match=msg):
            pages.save_corrections(bad, merged(cfg_path), cfg_path)
    items = [{"target": "Claude Code", "aliases": ["cloud code", "克拉德扣的"]}, {"target": "GitHub", "aliases": ["get hub"]}]
    assert pages.save_corrections(items, merged(cfg_path), cfg_path) == ["asr_corrections"]
    assert merged(cfg_path)["asr_corrections"] == {"Claude Code": ["cloud code", "克拉德扣的"], "GitHub": ["get hub"]}
    assert pages.save_corrections(items, merged(cfg_path), cfg_path) == []
    assert pages.save_corrections([], merged(cfg_path), cfg_path) == ["asr_corrections"]  # 清空
    assert merged(cfg_path)["asr_corrections"] == {}


async def test_hotwords_and_corrections_endpoints(cfg_path, monkeypatch):
    cfg_path.write_text(XASR_YAML, encoding="utf-8")
    loaded = []

    def fake_init(logger, config, **kwargs):
        loaded.append(config["asr_hotwords"])
        return {"asr": "new-asr"}
    monkeypatch.setattr(ah, "initialize_modules", fake_init)
    ws = SimpleNamespace(config_lock=asyncio.Lock(), _asr="old-asr")
    running = {"selected_module": {"ASR": "SherpaXASR"}, "ASR": {}}
    app = web.Application()
    handler = ah.AdminHandler(running, ws_server=ws, config_path=cfg_path)
    handler.register(app)
    fake = SimpleNamespace(session_id="s-9", device_id="dev-9", client_ip="x", config={})
    connections.register(fake)
    try:
        async with TestClient(TestServer(app)) as c:
            await c.post("/admin/api/setup", json={"password": "pass-1234"})
            r = await (await c.post("/admin/api/asr/hotwords", json={"words": ["GitHub", "Sonnet"]})).json()
            assert r["applied"] and "重新加载" in r["note"] and ws._asr == "new-asr" and loaded == [["GitHub", "Sonnet"]]
            assert running["asr_hotwords"] == ["GitHub", "Sonnet"] == fake.config["asr_hotwords"]
            assert (await c.post("/admin/api/asr/hotwords", json={"words": ["a,b"]})).status == 400
            r = await (await c.post("/admin/api/asr/corrections",
                                    json={"items": [{"target": "GitHub", "aliases": ["get hub"]}]})).json()
            assert r["note"] == "已保存，马上生效"
            assert fake.config["asr_corrections"] == {"GitHub": ["get hub"]} == running["asr_corrections"]
            d = await (await c.get("/admin/api/asr")).json()
            assert d["corrections"] == [{"target": "GitHub", "aliases": ["get hub"]}] and isinstance(d["recent"], list)
            await c.post("/admin/api/logout", json={})
            assert (await c.post("/admin/api/asr/corrections", json={"items": []})).status == 401
    finally:
        connections.unregister(fake)


def test_save_hotwords_moves_legacy_list(cfg_path):
    """旧版本存在 X-ASR 条目里的热词：读得到，保存时搬到顶层并删掉旧的。"""
    cfg_path.write_text(XASR_YAML + "ASR:\n  SherpaXASR:\n    hotwords: [Docker]\n", encoding="utf-8")
    assert pages.describe_hotwords(merged(cfg_path))["words"] == ["Docker"]
    assert pages.save_hotwords(["Docker"], merged(cfg_path), cfg_path) == ["hotwords"]  # 内容一样也要搬
    m = merged(cfg_path)
    assert m["asr_hotwords"] == ["Docker"] and "hotwords" not in m["ASR"]["SherpaXASR"]


# ---------- 豆包语音识别 ----------

DOUBAO_YAML = CUSTOM_YAML.replace("selected_module:\n", "selected_module:\n  ASR: DoubaoStreamASRV2\n")


def test_describe_doubao_defaults(cfg_path):
    d = pages.describe_asr(merged(cfg_path))
    assert d["doubao"]["entry"] == catalog.DOUBAO_ASR_ENTRY and d["doubao"]["configured"] is False  # 占位文本不算
    assert d["doubao"]["auth"] == "key" and d["doubao"]["resource_id"] == "volc.seedasr.sauc.duration"
    assert d["local"]["exists"] and d["choice"] in ("local", "other")
    cfg_path.write_text(DOUBAO_YAML, encoding="utf-8")
    assert pages.asr_choice(merged(cfg_path)) == "doubao"
    assert pages.describe_hotwords(merged(cfg_path))["supported"] is True  # 豆包也能用后台的热词


@pytest.mark.parametrize("payload, message", [
    ({"auth": "key"}, "API Key"),
    ({"auth": "key", "api_key": "a b"}, "空格"),
    ({"auth": "app", "access_token": "t"}, "APP ID"),
    ({"auth": "app", "appid": "123"}, "Access Token"),
    ({"auth": "key", "api_key": "k", "resource_id": "volc.other"}, "资源 ID"),
    ({"auth": "x"}, "鉴权方式"),
])
def test_save_doubao_validation(cfg_path, payload, message):
    with pytest.raises(ValueError, match=message):
        pages.save_doubao(payload, merged(cfg_path), cfg_path)


def test_save_doubao_auth_modes(cfg_path):
    key = "volc-key-0123456789"
    assert pages.save_doubao({"auth": "key", "api_key": key}, merged(cfg_path), cfg_path) == ["api_key"]
    d = pages.describe_doubao(merged(cfg_path))
    assert d["configured"] and d["auth"] == "key" and d["key_hint"] == "volc…6789"
    # 密钥留空表示不改
    assert pages.save_doubao({"auth": "key", "resource_id": "volc.seedasr.sauc.concurrent"},
                             merged(cfg_path), cfg_path) == ["resource_id"]
    # 换成旧版控制台：清掉 API Key，按 APP ID 鉴权
    changed = pages.save_doubao({"auth": "app", "appid": "123456", "access_token": "tok-abcdefghijk"},
                                merged(cfg_path), cfg_path)
    assert set(changed) == {"appid", "access_token", "api_key"}
    entry = merged(cfg_path)["ASR"]["DoubaoStreamASRV2"]
    assert entry["api_key"] == "" and entry["appid"] == "123456"
    d = pages.describe_doubao(merged(cfg_path))
    assert d["auth"] == "app" and d["configured"] and d["token_set"]
    # 测试连接的配置：页面上还没保存的值优先
    cfg = pages.doubao_test_config({"auth": "key", "api_key": "new-key"}, merged(cfg_path))
    assert cfg["api_key"] == "new-key" and cfg["resource_id"] == "volc.seedasr.sauc.concurrent"  # 没填时用已保存的
    cfg = pages.doubao_test_config({"auth": "app"}, merged(cfg_path))
    assert cfg["api_key"] == "" and cfg["access_token"] == "tok-abcdefghijk"


def test_describe_hotwords_doubao_dropped(cfg_path):
    words = [f"word{i:03d}" for i in range(40)]
    cfg_path.write_text(DOUBAO_YAML + "asr_hotwords: [" + ", ".join(words) + "]\n", encoding="utf-8")
    h = pages.describe_hotwords(merged(cfg_path))
    assert h["dropped"] and h["dropped"][-1] == "word039"  # 超出豆包上限，后面的带不上


async def test_asr_use_and_doubao_endpoints(cfg_path, monkeypatch):
    loaded = []

    def fake_init(logger, config, **kwargs):
        loaded.append(config["selected_module"]["ASR"])
        return {"asr": "new-asr"}
    monkeypatch.setattr(ah, "initialize_modules", fake_init)

    async def fake_check(config):
        return {"ok": True, "seconds": 0.3} if config.get("api_key") == "good-key" else {"ok": False, "error": "鉴权失败"}
    monkeypatch.setattr(doubao_stream, "check_connection", fake_check)
    monkeypatch.setattr(pages, "asr_model_check", lambda entry: {"ok": True, "detail": "ok"}
                        if entry.get("type") == "sherpa_onnx_local" else None)
    ws = SimpleNamespace(config_lock=asyncio.Lock(), _asr="old-asr")
    running = {"selected_module": {"ASR": "SherpaXASR"}, "ASR": {}}
    app = web.Application()
    ah.AdminHandler(running, ws_server=ws, config_path=cfg_path).register(app)
    async with TestClient(TestServer(app)) as c:
        await c.post("/admin/api/setup", json={"password": "pass-1234"})
        r = await c.post("/admin/api/asr/use", json={"choice": "doubao"})
        assert r.status == 400 and "还没配置好" in (await r.json())["error"]
        r = await c.post("/admin/api/asr/doubao/test", json={"auth": "key", "api_key": "bad-key"})
        assert r.status == 400 and (await r.json())["error"] == "鉴权失败"
        r = await (await c.post("/admin/api/asr/doubao/test", json={"auth": "key", "api_key": "good-key"})).json()
        assert r == {"ok": True, "seconds": 0.3}
        r = await (await c.post("/admin/api/asr/doubao", json={"auth": "key", "api_key": "good-key"})).json()
        assert r["changed"] == ["api_key"] and loaded == []  # 没在用，只保存不加载
        r = await (await c.post("/admin/api/asr/use", json={"choice": "doubao"})).json()
        assert r["ok"] and loaded == ["DoubaoStreamASRV2"] and running["selected_module"]["ASR"] == "DoubaoStreamASRV2"
        d = await (await c.get("/admin/api/asr")).json()
        assert d["choice"] == "doubao" and "good-key" not in str(d)  # Key 只写不读
        r = await (await c.post("/admin/api/asr/use", json={"choice": "local"})).json()
        assert r["ok"] and loaded[-1] == "SherpaXASR"
        assert (await c.post("/admin/api/asr/use", json={"choice": "funasr"})).status == 400


async def test_kiosk_tts(kiosk, monkeypatch):
    """只合成、不对话：返回 WAV；流式合成 501，合成失败 502，文字长度要合理。"""
    import io
    import wave

    from core.providers.tts.dto.dto import InterfaceType
    from core.utils import modules_initialize

    said = []

    class FakeTTS:
        interface_type = InterfaceType.NON_STREAM

        def synthesize_pcm(self, text, sample_rate=16000):
            said.append((text, sample_rate))
            return b"\x01\x00" * 2400
    monkeypatch.setattr(modules_initialize, "initialize_tts", lambda config: FakeTTS())
    r = await kiosk.post("/kiosk/tts", json={"text": " 该晾衣服了 "})
    assert r.status == 200 and r.content_type == "audio/wav"
    with wave.open(io.BytesIO(await r.read())) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, ah.KIOSK_TTS_RATE, 2400)
    assert said == [("该晾衣服了", ah.KIOSK_TTS_RATE)]
    for bad in ({}, {"text": "  "}, {"text": "字" * (ah.KIOSK_TTS_MAX_CHARS + 1)}):
        assert (await kiosk.post("/kiosk/tts", json=bad)).status == 400
    FakeTTS.synthesize_pcm = lambda self, text, sample_rate=16000: None   # 连不上合成服务
    assert (await kiosk.post("/kiosk/tts", json={"text": "该晾衣服了"})).status == 502
    FakeTTS.synthesize_pcm = lambda self, text, sample_rate=16000: 1 / 0
    assert (await kiosk.post("/kiosk/tts", json={"text": "该晾衣服了"})).status == 502
    FakeTTS.interface_type = InterfaceType.DUAL_STREAM   # 流式合成
    assert (await kiosk.post("/kiosk/tts", json={"text": "该晾衣服了"})).status == 501
