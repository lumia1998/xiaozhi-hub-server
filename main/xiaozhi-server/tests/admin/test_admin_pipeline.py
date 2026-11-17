"""管理后台"工作原理"页：五步流程和速查卡片按配置生成，提示词拆解的各段和变量标记。全部使用临时配置文件。"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin import config_store, connections, pipeline
from core.admin.admin_handler import AdminHandler

CFG_YAML = """\
selected_module:
  LLM: ClaudeAPI
  Intent: nointent
tts_audio_prebuffer_count: 15
LLM:
  ClaudeAPI:
    type: openai
    model_name: claude-haiku-5-5
call_names:
  models:
    DeepSeekLLM: [deepseek, 深度求索]
"""


API_YAML = """\
selected_module:
  LLM: DeepSeekLLM
  Intent: function_call
Intent:
  function_call:
    functions: [get_weather, play_music]
"""


@pytest.fixture
def cfg_path(tmp_path) -> Path:
    p = tmp_path / ".config.yaml"
    p.write_text(CFG_YAML, encoding="utf-8")
    return p


def merged(path):
    return config_store.load_merged(custom_path=path)


def marks(text):
    return re.findall(f"{pipeline.MARK_START}(\\w+){pipeline.MARK_SEP}", text)


def test_stages_follow_config(cfg_path):
    stages = {s["id"]: s for s in pipeline.describe_flow(merged(cfg_path))["stages"]}
    assert list(stages) == ["listen", "asr", "llm", "tts", "play"]
    assert stages["listen"]["value"] == "停顿 800ms 算说完"  # config.yaml 里 SileroVAD 的 min_silence_duration_ms
    assert stages["llm"]["value"] == "Claude" and stages["llm"]["hint"] == "claude-haiku-5-5"
    assert stages["play"]["value"] == "缓冲约 0.9 秒" and stages["play"]["page"] is None
    assert {s["page"] for s in stages.values()} == {"asr", "llm", "tts", None}  # 点击跳到对应的设置页


def test_tips_follow_config(cfg_path):
    tips = {t["title"]: t for t in pipeline.describe_flow(merged(cfg_path))["tips"]}
    assert list(tips) == ["唤醒", "换模型", "打断", "接着上次聊", "结束"]
    assert "第一次用时是 Claude" in tips["唤醒"]["result"]
    assert "“我在”" in tips["换模型"]["result"] and "深度求索" in tips["换模型"]["result"]  # 列出现在的称呼
    assert "最近 30 条记录" in tips["接着上次聊"]["result"] and "所有模型共用" in tips["接着上次聊"]["result"]
    assert "“退出”" in tips["结束"]["say"] and "2 分钟没人说话" in tips["结束"]["result"]


def test_tips_with_call_names_and_history_off(tmp_path):
    path = tmp_path / ".config.yaml"
    path.write_text(API_YAML + "call_names:\n  enabled: false\nchat_history:\n  enabled: false\n", encoding="utf-8")
    flow = pipeline.describe_flow(merged(path))
    tips = {t["title"]: t for t in flow["tips"]}
    assert "换模型" not in tips
    assert "DeepSeek" in tips["唤醒"]["result"]
    assert "断开后从头开始" in tips["接着上次聊"]["result"]
    assert next(s for s in flow["stages"] if s["id"] == "llm")["value"] == "DeepSeek"


def test_prompt_segments_without_plugins(cfg_path):
    p = pipeline.describe_prompt(merged(cfg_path))
    assert p["llm"] == "Claude"
    assert [s["id"] for s in p["segments"]] == ["base", "history"]
    base = p["segments"][0]
    found = set(marks(base["text"]))
    assert {"base_prompt", "today_date", "lunar_date", "emojiList", "language"} <= found
    assert not {"local_address", "weather_info"} & found  # 默认模板不按 IP 查位置和天气
    assert "<identity>" in base["text"] and "hub-base-prompt.txt" in base["note"]
    assert "data/chat.db" in p["segments"][1]["note"]  # 聊天记录默认落盘
    assert p["actual"] is None  # 没有在线设备


def test_prompt_segments_for_api_with_plugins(tmp_path):
    path = tmp_path / ".config.yaml"
    path.write_text(API_YAML, encoding="utf-8")
    p = pipeline.describe_prompt(merged(path))
    assert p["llm"] == "DeepSeek"
    assert [s["id"] for s in p["segments"]] == ["base", "tools", "history"]
    tools = next(s for s in p["segments"] if s["id"] == "tools")["text"]
    assert "查天气" in tools and "放音乐" in tools and "热点新闻" not in tools


def test_actual_prompt_from_online_device(cfg_path):
    msg = SimpleNamespace(role="system", content="真实的系统提示词")
    conn = SimpleNamespace(session_id="s-p", device_id="dev-p", client_ip="192.168.6.211",
                           dialogue=SimpleNamespace(dialogue=[msg]))
    connections.register(conn)
    try:
        p = pipeline.describe_prompt(merged(cfg_path))
        assert p["actual"] == {"device_id": "dev-p", "text": "真实的系统提示词"}
    finally:
        connections.unregister(conn)


async def test_pipeline_endpoint_requires_login(cfg_path):
    app = web.Application()
    AdminHandler({"selected_module": {"LLM": "ClaudeAPI"}}, config_path=cfg_path).register(app)
    async with TestClient(TestServer(app)) as c:
        assert (await c.get("/admin/api/pipeline")).status == 401
        await c.post("/admin/api/setup", json={"password": "pass-1234"})
        data = await (await c.get("/admin/api/pipeline")).json()
        assert data["ok"] and data["flow"]["stages"] and data["flow"]["tips"] and data["prompt"]["segments"]
        assert data["design_url"].endswith("hub-docs/design.md")
