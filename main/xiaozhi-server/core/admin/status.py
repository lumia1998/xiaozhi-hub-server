"""管理后台状态页：环境自检与当前运行信息。所有检查都是只读、快速的。"""

import ctypes.util
import shutil
import sys

from core.admin import config_store, deps, pages
from core.providers.llm.pi.diagnostics import check_runtime
from core.utils.util import get_local_ip

def _check(name, ok, detail="", level=None):
    return {"name": name, "level": level or ("ok" if ok else "error"), "detail": detail}


def _plugin_setting_checks(merged: dict) -> list:
    from core.admin import catalog, plugin_settings

    functions = [str(f) for f in ((merged.get("Intent") or {}).get("function_call") or {}).get("functions") or []]
    missing = [p["label"] for p in catalog.PLUGINS if p.get("settings") and p["name"] in functions
               and not plugin_settings.configured(merged, p["settings"])]
    if not missing or config_store.entry_type(merged, "Intent") != "function_call":
        return []
    return [_check("插件设置", False, f"已勾选{'、'.join(dict.fromkeys(missing))}，但还没填 API Key 等必填设置，"
                                     "请到「意图识别」页插件旁边的「设置」里填写", level="warn")]


def collect_status(config: dict, merged: dict) -> dict:
    """config 是服务正在使用的配置，merged 是磁盘上的最新配置。"""
    ip = get_local_ip()
    server = config.get("server", {})
    http_port = int(server.get("http_port", 8003))
    selected = dict(merged.get("selected_module") or {})
    running = dict(config.get("selected_module") or {})

    checks = []
    checks.append(_check("Python", sys.version_info[:2] == (3, 10), sys.version.split()[0],
                         level="ok" if sys.version_info[:2] == (3, 10) else "warn"))
    pi_level, pi_detail = check_runtime()
    checks.append(_check("Pi 引擎", pi_level == "ok", pi_detail, level=pi_level))
    ffmpeg = shutil.which("ffmpeg")
    checks.append(_check("ffmpeg", bool(ffmpeg), ffmpeg or "未找到，请安装 ffmpeg"))
    opus = ctypes.util.find_library("opus")
    try:
        import opuslib_next  # noqa: F401
        opus_ok = True
    except Exception as e:
        opus_ok, opus = False, f"{opus or ''} 加载失败: {e}"
    checks.append(_check("opus", opus_ok, opus or "已加载"))

    # 所选模块需要的 Python 包：同步上游后默认模块新增了依赖、或手动改配置选了其他模块时，
    # 精简安装可能缺包。后台不管理的 VAD、Memory、VLLM 也要查，否则服务会在启动时才报错
    lacking = []
    for section in (*config_store.SECTIONS, "VAD", "Memory", "VLLM"):
        name = selected.get(section)
        missing = deps.missing_packages(section, config_store.entry_type(merged, section)) if name else ()
        if missing:
            lacking.append(f"{name} 缺少 {', '.join(missing)}")
    checks.append(_check("模块依赖", not lacking,
                         "；".join(lacking) + "。请重新运行安装脚本补装依赖" if lacking else "所选模块的依赖均已安装"))

    # 语音识别模型（检查本地模型文件）
    model_check = pages.asr_model_check((merged.get("ASR") or {}).get(selected.get("ASR"), {}))
    if model_check:
        checks.append(_check("语音识别模型", model_check["ok"], model_check["detail"]))

    # 大模型
    llm_entry = (merged.get("LLM") or {}).get(selected.get("LLM"), {})
    secrets = [k for k in llm_entry if config_store.SECRET_FIELD.search(k)]
    missing = [k for k in secrets if config_store.is_placeholder(llm_entry.get(k))]
    if secrets:
        checks.append(_check("大模型密钥", not missing,
                             "已填写" if not missing else f"{selected.get('LLM')} 未填写 {', '.join(missing)}"))

    # 后台能设置的插件：勾选了、但必填的设置（如 API Key）没填，而且会用到插件时提醒
    checks.extend(_plugin_setting_checks(merged))

    return {
        "addresses": {
            "ip": ip,
            "ota": f"http://{ip}:{http_port}/xiaozhi/ota/",
            "admin": f"http://{ip}:{http_port}/admin",
        },
        "modules": {k: {"selected": selected.get(k), "type": config_store.entry_type(merged, k),
                        "running": running.get(k)} for k in config_store.SECTIONS},
        "checks": checks,
    }
