"""LLM 链路测试，只在 Live 模式（``RUN_LIVE_API_TESTS=1``）跑：调真实模型。

大模型都经 Pi 引擎，请求由它的 Node 子进程发出，conftest 在 httpx 传输层装的断网拦不住，
默认模式下跑也会真的请求模型，所以默认整个文件跳过。

接口约定（core/providers/llm/base.py）：
    response(session_id, dialogue) -> Iterator[str]，同步流式生成器
    response_no_stream(system_prompt, user_content) -> str

LLM 是同步 generator，不能直接 asyncio.wait_for，放进线程池加超时。
"""
from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

import pytest

from test.conftest import (
    LIVE_API_TESTS,
    get_provider_config,
    get_provider_type,
    get_selected_provider_name,
    has_real_key,
)

if not LIVE_API_TESTS:
    pytest.skip("Pi 经 Node 发请求，conftest 拦不住：只在 RUN_LIVE_API_TESTS=1 时跑", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "main" / "xiaozhi-server"))


def _build_provider():
    name = get_selected_provider_name("LLM")
    if not name:
        pytest.skip("selected_module.LLM 未设置")
    cfg = get_provider_config("LLM", name)
    if not cfg:
        pytest.skip(f"LLM provider '{name}' 配置缺失")
    # 模型服务的条目，Key 在 model_services 里
    if not cfg.get("service_id") and not has_real_key(cfg, "api_key"):
        pytest.skip(f"LLM '{name}' 的 api_key 未配置")
    ptype = get_provider_type("LLM", name)
    if not ptype:
        pytest.skip(f"LLM '{name}' 的 type 字段缺失")
    from core.utils.llm import create_instance

    return create_instance(ptype, cfg)


def _collect_with_timeout(provider, session_id: str, dialogue, timeout: float = 10.0):
    """调同步 generator response()，用线程池加 10s 超时。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: list(provider.response(session_id, dialogue)))
        return future.result(timeout=timeout)


def test_llm_response_streams_non_empty() -> None:
    """response(session_id, dialogue) 应产生非空 token 流。"""
    provider = _build_provider()
    dialogue = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "用一句话说 hi"},
    ]
    chunks = _collect_with_timeout(provider, "test-session", dialogue, timeout=10.0)

    full = "".join(chunks).strip()
    assert isinstance(full, str), f"LLM chunks join 应是 str，得到 {type(full).__name__}"
    assert full, f"LLM 返回空响应（provider={type(provider).__name__}）"


def test_llm_response_no_stream_returns_string() -> None:
    """response_no_stream 应返回非空字符串。"""
    provider = _build_provider()
    text = provider.response_no_stream("你是助手", "介绍一下北京").strip()

    assert isinstance(text, str), f"response_no_stream 应返回 str，得到 {type(text).__name__}"
    assert text, "response_no_stream 返回空字符串"


def test_llm_dialogue_min_length_one_user() -> None:
    """至少一条 user 消息的 dialogue 应能跑通。"""
    provider = _build_provider()
    dialogue = [{"role": "user", "content": "ping"}]
    chunks = _collect_with_timeout(provider, "s1", dialogue, timeout=10.0)

    assert "".join(chunks).strip(), "单 user 消息应返回内容"
