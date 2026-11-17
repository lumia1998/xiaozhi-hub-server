"""多模型路由的单元测试：按连接切换模型、各聊各的、记住上次的模型、连接关闭通知到每个用过的模型。"""

from types import SimpleNamespace

import pytest

from core.agent import llm_router
from core.agent.session_store import SessionStore
from core.utils import llm as llm_utils
from core.utils.dialogue import Dialogue, Message


class FakeLLM:
    def __init__(self, name, config=None):
        self.name, self.config = name, config
        self.closed = []

    def on_conn_close(self, conn):
        self.closed.append(conn.session_id)


def server_config():
    return {
        "selected_module": {"LLM": "ClaudeAPI"},
        "LLM": {
            "ClaudeAPI": {"type": "openai", "base_url": "https://api.anthropic.com/v1/", "model_name": "claude-haiku-5-5",
                          "api_key": "sk-ant"},
            "DeepSeekLLM": {"type": "openai", "url": "https://api.deepseek.com", "model_name": "deepseek-chat",
                            "api_key": "sk-123"},
            "KimiLLM": {"type": "openai", "base_url": "https://api.moonshot.cn/v1", "model_name": "kimi",
                        "api_key": "你的api_key"},
        },
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    created = []

    def create(kind, config):
        inst = FakeLLM(kind, config)
        created.append(inst)
        return inst

    monkeypatch.setattr(llm_utils, "create_instance", create)
    monkeypatch.setattr(llm_router, "_store", SessionStore(tmp_path / "agent_sessions.json", "last_model"))
    monkeypatch.setattr(llm_router, "_pool", {})
    default = FakeLLM("default")
    server = SimpleNamespace(config=server_config(), _llm=default)
    return SimpleNamespace(server=server, default=default, created=created)


def make_conn(env, sid="s1", device="dev-1"):
    dialogue = Dialogue()
    dialogue.put(Message(role="system", content="系统提示"))
    return SimpleNamespace(session_id=sid, device_id=device, server=env.server, llm=env.server._llm,
                           dialogue=dialogue, prompt="系统提示", config={})


def users(conn):
    return [m.content for m in conn.dialogue.dialogue if m.role == "user"]


def test_model_ready():
    cfg = server_config()
    assert llm_router.model_ready(cfg, "ClaudeAPI")
    assert llm_router.model_ready(cfg, "DeepSeekLLM")
    assert not llm_router.model_ready(cfg, "KimiLLM")  # Key 还是占位文字
    assert not llm_router.model_ready(cfg, "Nope")
    # 只认 Pi 的类型：已停用的 Claude Code、删掉的旧适配器、没写 type 的都不算
    assert not llm_router.model_ready({"LLM": {"ClaudeCLI": {"type": "claude_cli"}}}, "ClaudeCLI")
    filled = {"model_name": "qwen2.5", "base_url": "http://localhost:11434/v1"}
    assert not llm_router.model_ready({"LLM": {"OllamaLLM": {"type": "ollama", **filled}}}, "OllamaLLM")
    assert not llm_router.model_ready({"LLM": {"Bare": dict(filled)}}, "Bare")
    for kind in ("pi", "openai", "multi_api"):
        assert llm_router.model_ready({"LLM": {"M": {"type": kind, **filled}}}, "M"), kind
    assert not llm_router.model_ready({"LLM": {"M": {"type": "multi_api", "model_name": "m"}}}, "M")  # 没有接口地址


def test_switch_keeps_separate_dialogues(env):
    conn = make_conn(env)
    llm_router.on_conn_open(conn)
    conn.dialogue.put(Message(role="user", content="问 Claude"))

    assert llm_router.switch(conn, "DeepSeekLLM")
    deepseek = env.created[0]
    assert conn.llm is deepseek
    assert users(conn) == []  # 不带 Claude 的对话过去
    assert [m.content for m in conn.dialogue.dialogue if m.role == "system"] == ["系统提示"]
    conn.dialogue.put(Message(role="user", content="问 DeepSeek"))

    assert llm_router.switch(conn, "ClaudeAPI")
    assert conn.llm is env.default and users(conn) == ["问 Claude"]
    assert llm_router.switch(conn, "DeepSeekLLM")
    assert conn.llm is deepseek and users(conn) == ["问 DeepSeek"]  # 切回来还记得
    assert conn.hub_route["used"] == [env.default, deepseek]  # 用过的实例各记一次，连接关闭时逐个通知


def test_unready_model_does_not_switch(env):
    conn = make_conn(env)
    llm_router.on_conn_open(conn)
    assert llm_router.switch(conn, "KimiLLM") is False
    assert conn.llm is env.default and env.created == []
    assert llm_router.remembered(conn) is None


def test_remembers_last_model_per_device(env):
    conn = make_conn(env)
    llm_router.on_conn_open(conn)
    llm_router.switch(conn, "DeepSeekLLM")
    assert llm_router.remembered(conn) == "DeepSeekLLM"

    again = make_conn(env, sid="s2")  # 同一台设备重连
    llm_router.on_conn_open(again)
    assert again.llm is env.created[0] and again.hub_route["current"] == "DeepSeekLLM"
    other = make_conn(env, sid="s3", device="dev-2")  # 别的设备不受影响
    llm_router.on_conn_open(other)
    assert other.llm is env.default and other.hub_route["current"] == "ClaudeAPI"


def test_remembered_model_gone_falls_back_to_default(env):
    conn = make_conn(env)
    llm_router.on_conn_open(conn)
    llm_router.switch(conn, "DeepSeekLLM")
    env.server.config["LLM"]["DeepSeekLLM"]["api_key"] = ""  # 后台把 Key 清掉了
    again = make_conn(env, sid="s2")
    llm_router.on_conn_open(again)
    assert again.llm is env.default


def test_close_notifies_every_used_model(env):
    conn = make_conn(env)
    llm_router.on_conn_open(conn)
    llm_router.switch(conn, "DeepSeekLLM")
    llm_router.switch(conn, "ClaudeAPI")
    llm_router.on_conn_close(conn)
    assert env.default.closed == ["s1"] and env.created[0].closed == ["s1"]


def test_instances_shared_and_rebuilt_when_config_changes(env):
    a, b = make_conn(env, "s1", "d1"), make_conn(env, "s2", "d2")
    for c in (a, b):
        llm_router.on_conn_open(c)
        llm_router.switch(c, "DeepSeekLLM")
    assert a.llm is b.llm and len(env.created) == 1  # 所有连接共用

    env.server.config["LLM"]["DeepSeekLLM"]["model_name"] = "deepseek-reasoner"  # 后台保存后热加载
    c = make_conn(env, "s3", "d3")
    llm_router.on_conn_open(c)
    llm_router.switch(c, "DeepSeekLLM")
    assert len(env.created) == 2 and c.llm is env.created[1]
    assert a.llm is env.created[0]  # 在线连接继续用旧实例


def test_calling_current_model_again_uses_new_config(env):
    """已经在用某个模型，后台改了它的设置后再呼名：换成照新配置建的实例，这个模型的对话保留。"""
    conn = make_conn(env)
    llm_router.on_conn_open(conn)
    llm_router.switch(conn, "DeepSeekLLM")
    conn.dialogue.put(Message(role="user", content="问 DeepSeek"))
    old = conn.llm
    assert llm_router.switch(conn, "DeepSeekLLM") and conn.llm is old and len(env.created) == 1  # 没改：不重建

    env.server.config["LLM"]["DeepSeekLLM"]["api_key"] = "sk-new"
    assert llm_router.switch(conn, "DeepSeekLLM")
    new = env.created[1]
    assert conn.llm is new and new.config["api_key"] == "sk-new"
    assert users(conn) == ["问 DeepSeek"]
    llm_router.on_conn_close(conn)
    assert old.closed == ["s1"] and new.closed == ["s1"]  # 新旧实例都通知到


def test_default_model_uses_server_instance(env):
    env.server.config["selected_module"]["LLM"] = "DeepSeekLLM"
    env.server._llm = FakeLLM("deepseek-default")
    conn = make_conn(env)
    conn.llm = env.server._llm
    llm_router.on_conn_open(conn)
    assert llm_router.switch(conn, "ClaudeAPI") and conn.llm is env.created[0]
    assert llm_router.switch(conn, "DeepSeekLLM") and conn.llm is env.server._llm
