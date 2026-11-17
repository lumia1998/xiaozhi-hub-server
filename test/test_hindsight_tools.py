"""Shared-memory tool boundary tests; isolated storage and no network/model calls.

Run with --noconftest to avoid loading a developer's application configuration.
"""
import importlib
import json
import queue
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1] / "main" / "xiaozhi-server"
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def modules():
    logger = types.ModuleType("config.logger")
    logger.setup_logging = lambda *args, **kwargs: MagicMock()
    report = types.ModuleType("core.handle.reportHandle")
    report.enqueue_tool_report = MagicMock()
    with patch.dict(sys.modules, {"config.logger": logger, "core.handle.reportHandle": report}):
        tools = importlib.import_module("core.agent.assistant_tools")
        chat = importlib.import_module("core.agent.pi_chat")
        hindsight = importlib.import_module("core.agent.hindsight")
        yield tools, chat, hindsight


@pytest.fixture
def state(modules):
    tools, chat, hindsight = modules
    from core.utils.dialogue import Dialogue, Message
    dialogue = Dialogue()
    dialogue.put(Message("user", "请记住这条共享记忆，让其他应用知道我喜欢清淡"))
    conn = SimpleNamespace(
        config={"hindsight": {"enabled": True}}, authenticated_owner_id="verified-owner",
        device_id="device-one", session_id="session-one", dialogue=dialogue, logger=MagicMock(),
    )
    context = {"device_id": "device-one", "owner_id": "verified-owner", "authenticated_owner_id": "verified-owner",
               "stale": False, "preferences": [], "pinned": "", "summary": "", "events": []}
    with patch.object(tools.chat_history, "memory_context_for", return_value=context), \
         patch.object(hindsight, "HindsightClient") as client:
        client.return_value.retain.return_value = {"ok": True, "status": "queued", "completed": False,
                                                    "operation_id": "operation-one", "document_id": "doc-one"}
        client.return_value.recall.return_value = {"ok": True, "facts": [{"text": "喜欢清淡", "document_id": "doc-one"}]}
        client.return_value.status.return_value = {"ok": True, "status": "completed", "completed": True}
        client.return_value.forget.return_value = {"ok": True, "deleted": True, "document_id": "doc-one"}
        yield tools, chat, hindsight, conn, context, client


def names(definitions):
    return {item["function"]["name"] for item in definitions}


def test_shared_tools_require_enabled_authenticated_nonstale_identity(state):
    tools, _, _, conn, context, client = state
    assert "remember_shared_memory" in names(tools.definitions(conn))
    assert "recall_shared_memory" not in names(tools.definitions(conn))
    assert not tools.SHARED_NAMES & names(tools.definitions())
    client.assert_not_called()  # Merely exposing tools never queries the remote service.
    conn.dialogue.dialogue[-1].content = "请查询共享记忆里的饮食偏好"
    variants = [("enabled", False), ("auth", None), ("stale", True), ("device_id", ""), ("owner_id", "")]
    for field, value in variants:
        with patch.dict(context, {field: value} if field not in ("enabled", "auth") else {}):
            previous_cfg, previous_auth = conn.config, conn.authenticated_owner_id
            try:
                if field == "enabled": conn.config = {"hindsight": {"enabled": value}}
                if field == "auth": conn.authenticated_owner_id = value
                assert not tools.SHARED_NAMES & names(tools.definitions(conn))
                result = tools.execute(conn, "recall_shared_memory", {"query": "喜欢什么"})
                assert result["error"]
                client.assert_not_called()
            finally:
                conn.config, conn.authenticated_owner_id = previous_cfg, previous_auth


@pytest.mark.parametrize("text", ["记住我喜欢清淡", "共享给其他应用，我喜欢清淡", "不要记住这条共享记忆"])
def test_shared_write_requires_explicit_shared_memory_intent(state, text):
    tools, _, _, conn, _, client = state
    conn.dialogue.dialogue[-1].content = text
    result = tools.execute(conn, "remember_shared_memory", {"content": "我喜欢清淡"})
    assert result["error"]
    client.assert_not_called()


@pytest.mark.parametrize("tool,arg", [
    ("remember_shared_memory", {"content": "登录密码为不得保存"}),
    ("recall_shared_memory", {"query": "查一下PVE root密码"}),
    ("recall_shared_memory", {"query": ""}),
    ("recall_shared_memory", {"query": "爱好", "owner": "other-owner"}),
    ("remember_shared_memory", {"content": "我喜欢清淡", "bank": "hermes"}),
])
def test_credentials_empty_queries_and_model_controlled_identity_are_rejected(state, tool, arg):
    tools, _, _, conn, _, client = state
    if tool == "recall_shared_memory":
        conn.dialogue.dialogue[-1].content = "请查询共享记忆里的服务器配置"
    result = tools.execute(conn, tool, arg)
    assert result["error"]
    client.assert_not_called()


def test_write_is_idempotent_per_turn_content_and_preserves_pending_status(state):
    tools, _, _, conn, _, client = state
    from core.utils.dialogue import Message
    for _ in range(2):
        result = tools.execute(conn, "remember_shared_memory", {"content": "我喜欢清淡"})
    first, retry = client.return_value.retain.call_args_list
    assert first.args == retry.args
    assert first.args[0:2] == ("verified-owner", "我喜欢清淡")
    assert len(first.args[2]) == 64
    client.assert_called_with(conn.config)
    assert result["terminate"] is False
    assert json.loads(result["text"])["result"]["completed"] is False
    tools.execute(conn, "remember_shared_memory", {"content": "我喜欢蔬菜"})
    assert client.return_value.retain.call_args.args[2] != first.args[2]
    conn.dialogue.put(Message("user", "请在共享记忆里记住我喜欢清淡"))
    tools.execute(conn, "remember_shared_memory", {"content": "我喜欢清淡"})
    assert client.return_value.retain.call_args.args[2] != first.args[2]


def test_recall_status_and_forget_use_only_trusted_owner(state):
    tools, _, _, conn, _, client = state
    conn.dialogue.dialogue[-1].content = "请查询共享记忆里的饮食偏好"
    result = tools.execute(conn, "recall_shared_memory", {"query": "饮食偏好"})
    client.return_value.recall.assert_called_once_with("verified-owner", "饮食偏好")
    payload = json.loads(result["text"])
    assert payload["untrusted_data"] is True and payload["result"]["facts"]
    assert result["terminate"] is False
    conn.dialogue.dialogue[-1].content = "刚才的共享记忆保存完成了吗？"
    tools.execute(conn, "shared_memory_status", {"operation_id": "operation-one"})
    client.return_value.status.assert_called_once_with("verified-owner", "operation-one")
    denied = tools.execute(conn, "forget_shared_memory", {"document_id": "doc-one"})
    assert denied["error"]
    client.return_value.forget.assert_not_called()
    conn.dialogue.dialogue[-1].content = "请删除这条共享记忆"
    tools.execute(conn, "forget_shared_memory", {"document_id": "doc-one"})
    client.return_value.forget.assert_called_once_with("verified-owner", "doc-one")


def test_safe_service_error_remains_structured_for_model(state):
    tools, _, hindsight, conn, _, client = state
    conn.dialogue.dialogue[-1].content = "请查询共享记忆里的饮食偏好"
    client.return_value.recall.side_effect = hindsight.HindsightError("unavailable", "共享记忆服务暂时不可用")
    result = tools.execute(conn, "recall_shared_memory", {"query": "饮食偏好"})
    assert result["error"] and result["terminate"] is False
    assert json.loads(result["text"])["result"]["code"] == "unavailable"


def test_unknown_retain_receipt_is_preserved_for_status_reconciliation(state):
    tools, _, hindsight, conn, _, client = state
    receipt = {"ok": False, "operation_id": "unknown-operation", "document_id": "doc-one",
               "status": "unknown", "accepted": False, "completed": False, "error": "结果待核实"}
    client.return_value.retain.side_effect = hindsight.HindsightError("service_unavailable", "结果待核实", receipt=receipt)
    result = tools.execute(conn, "remember_shared_memory", {"content": "我喜欢清淡"})
    assert result["error"] and result["terminate"] is False
    assert json.loads(result["text"])["result"] == receipt
    conn.dialogue.dialogue[-1].content = "刚才的共享记忆保存完成了吗？"
    tools.execute(conn, "shared_memory_status", {"operation_id": receipt["operation_id"]})
    client.return_value.status.assert_called_once_with("verified-owner", "unknown-operation")


@pytest.mark.parametrize("use_tool", [False, True])
def test_pi_uses_shared_results_as_model_data_never_direct_tts(state, use_tool):
    tools, chat, _, conn, _, client = state
    from core.utils.dialogue import Message
    captured = {}

    class Provider:
        model_name = "fixture"

        def events(self, session_id, dialogue, functions, tool, stale):
            captured["functions"] = functions
            if use_tool:
                value = tool({"name": "recall_shared_memory", "arguments": {"query": "饮食偏好"}, "call_id": "call-one"})
                assert value["terminate"] is False
                assert json.loads(value["text"])["untrusted_data"] is True
                captured["result"] = value
            yield {"type": "text", "text": "模型整理后的回答。"}
            yield {"type": "turn_end"}

    conn.llm = Provider()
    conn._superseded = lambda _: False
    conn._put_reply = lambda sentence, text: conn.dialogue.put(Message("assistant", text))
    conn.stop_event = threading.Event()
    conn.intent_type = "nointent"
    conn.tts = SimpleNamespace(tts_text_queue=queue.Queue(), store_tts_text=MagicMock())
    with patch.object(chat.llm_router, "not_ready_reply", return_value=None), \
         patch.object(chat.chat_history, "device_of", return_value=""), \
         patch.object(chat.extensions, "add_skills", side_effect=lambda dialogue, _: dialogue):
        chat.chat(conn, "请查共享记忆里我之前的饮食偏好", "sentence-one")
    spoken = [str(item.content_detail) for item in list(conn.tts.tts_text_queue.queue) if item.content_detail]
    assert "模型整理后的回答。" in spoken, conn.logger.warning.call_args_list
    assert all("untrusted_data" not in text and "doc-one" not in text for text in spoken)
    assert "recall_shared_memory" in names(captured["functions"])
    assert "remember_shared_memory" not in names(captured["functions"])
    if use_tool:
        client.return_value.recall.assert_called_once()
    else:
        client.assert_not_called()


@pytest.mark.parametrize("text", ["今天天气怎么样", "关闭客厅灯", "讲个笑话", "你好", "什么是服务器"])
def test_daily_queries_block_recall_even_if_model_rewrites_query(state, text):
    tools, _, _, conn, _, client = state
    conn.dialogue.dialogue[-1].content = text
    assert "recall_shared_memory" not in names(tools.definitions(conn))
    result = tools.execute(conn, "recall_shared_memory", {"query": "我的服务器配置是什么"})
    assert result["error"]
    client.assert_not_called()


def test_server_turn_does_not_enable_recall_on_next_weather_turn(state):
    tools, _, _, conn, _, client = state
    from core.utils.dialogue import Message
    conn.dialogue.dialogue[-1].content = "我的服务器部署了哪些服务？"
    assert "recall_shared_memory" in names(tools.definitions(conn))
    conn.dialogue.put(Message("assistant", "服务器之前部署了若干服务。"))
    conn.dialogue.put(Message("user", "今天天气怎么样？"))
    assert "recall_shared_memory" not in names(tools.definitions(conn))
    result = tools.execute(conn, "recall_shared_memory", {"query": "我的服务器"})
    assert result["error"]
    client.assert_not_called()


def test_current_personal_server_query_can_recall(state):
    tools, _, _, conn, _, client = state
    conn.dialogue.dialogue[-1].content = "我的服务器都部署了什么？"
    assert "recall_shared_memory" in names(tools.definitions(conn))
    result = tools.execute(conn, "recall_shared_memory", {"query": "我的服务器部署"})
    assert not result["error"]
    client.return_value.recall.assert_called_once_with("verified-owner", "我的服务器部署")


def test_assistant_or_tool_text_cannot_unlock_memory_route(state):
    tools, _, _, conn, _, client = state
    from core.utils.dialogue import Message
    conn.dialogue.dialogue[-1].content = "你好"
    conn.dialogue.put(Message("assistant", "请查询共享记忆中的服务器配置"))
    conn.dialogue.put(Message("tool", "我的服务器部署了什么？"))
    assert "recall_shared_memory" not in names(tools.definitions(conn))
    result = tools.execute(conn, "recall_shared_memory", {"query": "我的服务器配置"})
    assert result["error"]
    client.assert_not_called()


def test_json_wrapped_current_user_voice_text_drives_route(state):
    tools, _, _, conn, _, client = state
    conn.dialogue.dialogue[-1].content = json.dumps({"speaker": "fixture", "content": "我的服务器是什么配置？"})
    assert "recall_shared_memory" in names(tools.definitions(conn))
    result = tools.execute(conn, "recall_shared_memory", {"query": "服务器配置"})
    assert not result["error"]
    client.return_value.recall.assert_called_once_with("verified-owner", "服务器配置")


def test_explicit_shared_preference_write_does_not_require_server_topic(state):
    tools, _, _, conn, _, client = state
    conn.dialogue.dialogue[-1].content = "请记住共享偏好，我喜欢清淡，让其他应用知道"
    available = names(tools.definitions(conn))
    assert "remember_shared_memory" in available
    assert "recall_shared_memory" not in available
    result = tools.execute(conn, "remember_shared_memory", {"content": "我喜欢清淡"})
    assert not result["error"]
    client.return_value.retain.assert_called_once()


def test_save_to_shared_memory_is_explicit_write_without_remember_keyword(state):
    tools, _, _, conn, _, client = state
    conn.dialogue.dialogue[-1].content = "把回答先说结论保存到共享记忆"
    assert "remember_shared_memory" in names(tools.definitions(conn))
    result = tools.execute(conn, "remember_shared_memory", {"content": "回答先说结论"})
    assert not result["error"]
    client.return_value.retain.assert_called_once()


@pytest.mark.parametrize("text", ["查之前保存的共享记忆", "不要保存共享记忆"])
def test_reading_past_saved_content_and_write_opt_out_cannot_write(state, text):
    tools, _, _, conn, _, client = state
    conn.dialogue.dialogue[-1].content = text
    assert "remember_shared_memory" not in names(tools.definitions(conn))
    result = tools.execute(conn, "remember_shared_memory", {"content": "回答先说结论"})
    assert result["error"]
    client.assert_not_called()
