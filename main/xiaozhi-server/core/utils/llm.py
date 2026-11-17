"""大模型实例：都经 Pi 引擎（core/providers/llm/pi）。"""

# openai、multi_api 是迁移到 Pi 之前的类型名，照样认
PI_TYPES = ("pi", "openai", "multi_api")


def create_instance(class_name, *args, **kwargs):
    if class_name not in PI_TYPES:
        raise ValueError(f"不支持的大模型类型：{class_name}。大模型都经 Pi 引擎，请在管理后台的“大模型”页新增模型服务")
    from core.providers.llm.pi.pi import LLMProvider

    return LLMProvider(*args, **kwargs)
