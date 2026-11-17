"""intent、llm、memory 的 create_instance：不认识的类型报 ValueError；大模型的类型都建 Pi。"""
import pytest

from core.utils import intent, llm, memory


@pytest.mark.parametrize("factory,module_type", [
    (intent.create_instance, "intent"),
    (llm.create_instance, "llm"),
    (memory.create_instance, "memory"),
])
def test_create_instance_raises_for_unknown_class(factory, module_type):
    with pytest.raises(ValueError) as exc_info:
        factory("definitely-not-a-real-provider-name-xyz")
    assert "不支持" in str(exc_info.value) or "不支持的" in str(exc_info.value)


@pytest.mark.parametrize("factory,module_type", [
    (intent.create_instance, "intent"),
    (llm.create_instance, "llm"),
    (memory.create_instance, "memory"),
])
def test_create_instance_accepts_args_and_kwargs(factory, module_type):
    """Even when the provider exists, the call must accept *args and **kwargs.

    We don't need to assert successful creation — just that the signature
    is variadic. We use a sentinel that will fail the path check.
    """
    with pytest.raises(ValueError):
        factory("__missing__", "positional-arg", kwarg="value")


def test_llm_types_all_build_pi(monkeypatch):
    from core.providers.llm.pi import pi

    monkeypatch.setattr(pi.BRIDGE, "start", lambda: None)  # 不起 Node
    for kind in llm.PI_TYPES:
        inst = llm.create_instance(kind, {"model_name": "deepseek-chat", "base_url": "https://api.deepseek.com"})
        assert isinstance(inst, pi.LLMProvider), kind
    with pytest.raises(ValueError, match="Pi"):
        llm.create_instance("ollama", {"model_name": "qwen2.5"})
