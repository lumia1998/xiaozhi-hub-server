"""管理后台各页面的读取与保存逻辑（大模型、语音识别、语音合成、意图识别）。

和 config_store 的通用条目编辑不同，这里按页面组织：只暴露后台要管理的内容，
并对取值做校验（例如说完停顿时长要在 200～5000 毫秒之间），错误信息直接给用户看。
所有函数只读写配置文件，不碰运行中的服务；热加载由 AdminHandler 负责。
"""

import re
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from config.config_loader import get_project_dir
from core.admin import catalog, config_store, plugin_settings, tts_settings
from core.agent import asr_text


def _selected(merged: dict, section: str):
    return (merged.get("selected_module") or {}).get(section)


def _entry(merged: dict, section: str, name: str) -> dict:
    entry = (merged.get(section) or {}).get(name)
    return entry if isinstance(entry, dict) else {}


def _text(value) -> str:
    return "" if config_store.is_placeholder(value) else str(value).strip()


# ---------- 大模型 ----------

def model_choices(merged: dict) -> list:
    """能切过去的大模型（设备上的模型列表、后台的呼名都用它）：填好了的 API 厂商条目在前，按 LLM_VENDORS 的顺序；
    type 是 pi、multi_api 的其他条目（模型服务里的模型）在后，vendor 是 custom。"""
    from core.agent import llm_router

    llm = merged.get("LLM") or {}
    choices = [{"entry": v["entry"], "vendor": v["id"], "label": v["label"], "model": _text(llm[v["entry"]].get("model_name"))}
               for v in catalog.LLM_VENDORS if llm_router.model_ready(merged, v["entry"])]
    listed = {c["entry"] for c in choices}
    # 不列 type: openai 的：config.yaml 里上游的示例条目（如 LMStudioLLM）不用填就算填好了
    choices += [{"entry": name, "vendor": "custom", "label": _text(e.get("model_name")), "model": _text(e.get("model_name"))}
                for name, e in llm.items()
                if name not in listed and e.get("type") in ("multi_api", "pi") and llm_router.model_ready(merged, name)]
    return choices


# ---------- 语音识别 ----------

# sherpa-onnx 各 model_type 需要的文件，第一个是主模型文件（用来判断有没有下载完整）。
# 和 core/providers/asr/sherpa_onnx_local.py 的 EXTRA_MODEL_FILES 保持一致；那边会导入 sherpa_onnx 等重的库，后台不直接引用
SHERPA_MODEL_FILES = {
    "sense_voice": ["model.int8.onnx", "tokens.txt"],
    "paraformer": ["model.int8.onnx", "tokens.txt"],
    "fire_red_asr": ["encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"],
    "fire_red_asr_ctc": ["model.int8.onnx", "tokens.txt"],
    "funasr_nano": ["llm.int8.onnx", "encoder_adaptor.int8.onnx", "embedding.int8.onnx", "Qwen3-0.6B"],
    "qwen3_asr": ["decoder.int8.onnx", "encoder.int8.onnx", "conv_frontend.onnx", "tokenizer"],
    "transducer": ["encoder*.onnx", "decoder*.onnx", "joiner*.onnx", "tokens.txt"],
}
SHERPA_MODEL_LABELS = {
    "sense_voice": "SenseVoice",
    "paraformer": "Paraformer",
    "fire_red_asr": "FireRedASR2-AED",
    "fire_red_asr_ctc": "FireRedASR2-CTC",
    "funasr_nano": "Fun-ASR-Nano",
    "qwen3_asr": "Qwen3-ASR",
    "transducer": "Zipformer Transducer",
}


def _find_model_file(model_dir: Path, name: str):
    """name 带 * 时按通配符找，优先 int8；找不到返回 None。"""
    if "*" not in name:
        path = model_dir / name
        return path if path.exists() else None
    matches = sorted(model_dir.glob(name), key=lambda p: (".int8." not in p.name, p.name))
    return matches[0] if matches else None


def asr_model_check(asr_entry: dict):
    """本地 ASR 的模型文件检查；不是本地模型时返回 None。"""
    if asr_entry.get("type") != "sherpa_onnx_local":
        return None
    files = SHERPA_MODEL_FILES.get(asr_entry.get("model_type") or "sense_voice", SHERPA_MODEL_FILES["sense_voice"])
    model_dir = Path(get_project_dir()) / (asr_entry.get("model_dir") or "")
    model = _find_model_file(model_dir, files[0]) or model_dir / files[0]
    size = model.stat().st_size if model.exists() else 0
    absent = [f for f in files if _find_model_file(model_dir, f) is None]
    ok = size > 100 * 1024 * 1024 and not absent
    if size and not absent:
        detail = f"{model.name} {size // (1024 * 1024)}MB"
    elif absent:
        detail = f"缺少 {model_dir / absent[0]}"
    else:
        detail = f"{model} 文件不完整"
    return {"ok": ok, "detail": detail}


def asr_label(entry: dict) -> str:
    if entry.get("type") != "sherpa_onnx_local":
        return "云端或其他识别服务"
    model_type = entry.get("model_type") or "sense_voice"
    name = SHERPA_MODEL_LABELS.get(model_type, model_type)
    if "x-asr" in str(entry.get("model_dir") or "").lower():
        name = "X-ASR 中英混合"
    return f"本地识别 · {name}（sherpa-onnx），不需要联网"


def asr_choice(merged: dict) -> str:
    """当前用的是后台能选的哪一种：local（本地 X-ASR）、doubao，或 other（手动改了配置）。"""
    name = _selected(merged, "ASR")
    entry = _entry(merged, "ASR", name)
    if name == catalog.LOCAL_ASR_ENTRY and entry.get("type") == "sherpa_onnx_local":
        return "local"
    if name == catalog.DOUBAO_ASR_ENTRY and entry.get("type") == "doubao_stream":
        return "doubao"
    return "other"


def describe_asr(merged: dict) -> dict:
    name = _selected(merged, "ASR")
    entry = _entry(merged, "ASR", name)
    info = [{"label": "模型目录", "value": str(entry.get("model_dir") or "")}] if entry.get("model_dir") else []
    if entry.get("model_type"):
        info.append({"label": "模型类型", "value": str(entry["model_type"])})
    local = _entry(merged, "ASR", catalog.LOCAL_ASR_ENTRY)
    return {
        "selected": name,
        "label": asr_label(entry),
        "info": info,
        "model": asr_model_check(entry),
        "choice": asr_choice(merged),
        "local": {"exists": local.get("type") == "sherpa_onnx_local", "label": asr_label(local),
                  "model": asr_model_check(local)},
        "doubao": describe_doubao(merged),
        "vad": describe_vad(merged),
        "hotwords": describe_hotwords(merged),
        "corrections": describe_corrections(merged),
        "recent": asr_text.recent(),
    }


# ---------- 豆包流式语音识别 ----------
# 新版火山引擎控制台只给一个 API Key；旧版控制台给 APP ID + Access Token。填了 API Key 就按新版鉴权

def describe_doubao(merged: dict) -> dict:
    entry = _entry(merged, "ASR", catalog.DOUBAO_ASR_ENTRY)
    key, token = entry.get("api_key"), entry.get("access_token")
    key_set, token_set = not config_store.is_placeholder(key), not config_store.is_placeholder(token)
    appid = _text(entry.get("appid"))
    resource = _text(entry.get("resource_id")) or catalog.DOUBAO_RESOURCES[0]["value"]
    exists = entry.get("type") == "doubao_stream"
    return {
        "entry": catalog.DOUBAO_ASR_ENTRY,
        "auth": "app" if not key_set and appid else "key",
        "key_set": key_set,
        "key_hint": config_store.mask_key(key),
        "appid": appid,
        "token_set": token_set,
        "token_hint": config_store.mask_key(token),
        "resource_id": resource,
        "resources": catalog.DOUBAO_RESOURCES,
        "configured": exists and (key_set or bool(appid and token_set)),
        "conflict": bool(entry) and not exists,
    }


def _doubao_values(payload: dict, entry: dict) -> dict:
    """校验页面提交的值，返回 {auth, api_key, appid, access_token, resource_id}；密钥留空时用已保存的。"""
    payload = payload or {}
    auth = str(payload.get("auth") or "key")
    if auth not in ("key", "app"):
        raise ValueError("鉴权方式只能是 API Key 或 APP ID + Access Token")
    resource = (str(payload.get("resource_id") or "").strip() or _text(entry.get("resource_id"))
                or catalog.DOUBAO_RESOURCES[0]["value"])
    if resource not in catalog.DOUBAO_RESOURCE_VALUES:
        raise ValueError("不支持这个资源 ID")
    values = {"auth": auth, "resource_id": resource}
    for field, label in (("api_key", "API Key"), ("access_token", "Access Token")):
        raw = str(payload.get(field) or "").strip()
        if any(c.isspace() for c in raw):
            raise ValueError(f"{label} 里不能有空格或换行")
        values[field] = raw or _text(entry.get(field))
    values["appid"] = str(payload.get("appid") or "").strip() or _text(entry.get("appid"))
    if auth == "key" and not values["api_key"]:
        raise ValueError("请填写 API Key")
    if auth == "app":
        if not re.fullmatch(r"[A-Za-z0-9]+", values["appid"]):
            raise ValueError("请填写 APP ID（只有字母和数字）")
        if not values["access_token"]:
            raise ValueError("请填写 Access Token")
    return values


def save_doubao(payload: dict, merged: dict, path: Path = None) -> list:
    """保存豆包的鉴权和资源 ID，返回修改的字段。用 APP ID 鉴权时清掉 API Key（有 API Key 时优先用它）。"""
    entry = _entry(merged, "ASR", catalog.DOUBAO_ASR_ENTRY)
    if entry and entry.get("type") != "doubao_stream":
        raise ValueError(f"配置里的 {catalog.DOUBAO_ASR_ENTRY} 不是豆包流式识别，请在配置文件里改名或删掉它")
    values = _doubao_values(payload, entry)
    wanted = {"resource_id": values["resource_id"]}
    if values["auth"] == "key":
        wanted["api_key"] = values["api_key"]
    else:
        wanted.update(appid=values["appid"], access_token=values["access_token"], api_key="")
    changes = {k: v for k, v in wanted.items() if v != _text(entry.get(k))}
    if not entry:
        changes = {"type": "doubao_stream", "enable_multilingual": False, "end_window_size": 200,
                   "output_dir": "tmp/", **changes}
    if not changes:
        return []
    with config_store.edit_custom(path) as data:
        target = data.setdefault("ASR", CommentedMap()).setdefault(catalog.DOUBAO_ASR_ENTRY, CommentedMap())
        for field, value in changes.items():
            target[field] = value
    return [f for f in changes if f in wanted]


def doubao_test_config(payload: dict, merged: dict) -> dict:
    """测试连接用的配置：页面上还没保存的值优先。"""
    entry = _entry(merged, "ASR", catalog.DOUBAO_ASR_ENTRY)
    values = _doubao_values(payload, entry)
    config = {k: v for k, v in entry.items() if k != "type"}
    config.update(resource_id=values["resource_id"], api_key=values["api_key"] if values["auth"] == "key" else "",
                  appid=values["appid"], access_token=values["access_token"])
    return config


# ---------- 热词 ----------
# 所有支持热词的识别共用一份，存在配置顶层的 asr_hotwords（见 core/agent/asr_text.py）
HOTWORD_MAX = 200
HOTWORD_BAD = re.compile(r"[,，\n\r\t]")
hotwords_supported = asr_text.hotwords_supported


def describe_hotwords(merged: dict) -> dict:
    entry = _entry(merged, "ASR", _selected(merged, "ASR"))
    configured = asr_text.hotwords_of(merged)
    seen = {w.lower() for w in configured}
    result = {
        "supported": hotwords_supported(entry),
        "words": configured,
        "auto": [w for w in asr_text.call_name_hotwords(merged) if w.lower() not in seen],
        "dropped": [],
    }
    if entry.get("type") == "doubao_stream":
        multilingual = str(entry.get("enable_multilingual", False)).lower() != "false"
        _, result["dropped"] = asr_text.doubao_hotwords(asr_text.effective_hotwords(entry, merged), multilingual)
    return result


def save_hotwords(words, merged: dict, path: Path = None) -> list:
    """把热词整个写进 data/.config.yaml 的 asr_hotwords，同时删掉旧版本存在识别条目里的。返回修改的字段。"""
    if not hotwords_supported(_entry(merged, "ASR", _selected(merged, "ASR"))):
        raise ValueError("当前语音识别不支持热词（支持的有 X-ASR、豆包流式语音识别）")
    if not isinstance(words, list):
        raise ValueError("热词格式不对")
    result = []
    for w in words:
        w = re.sub(r"\s+", " ", str(w)).strip()
        if not w:
            continue
        if HOTWORD_BAD.search(w) or len(w) > 40:
            raise ValueError(f"热词“{w}”不能包含逗号或换行，长度不超过 40 个字")
        if w.lower() not in {x.lower() for x in result}:
            result.append(w)
    if len(result) > HOTWORD_MAX:
        raise ValueError(f"热词最多 {HOTWORD_MAX} 个；太多会让识别变慢，也更容易误认")
    if result == asr_text.hotwords_of(merged) and "asr_hotwords" in merged:
        return []
    with config_store.edit_custom(path) as data:
        data["asr_hotwords"] = CommentedSeq(result)
        for entry in (data.get("ASR") or {}).values():
            if isinstance(entry, dict):
                entry.pop("hotwords", None)
    return ["hotwords"]


# ---------- 识别后纠错替换 ----------

def describe_corrections(merged: dict) -> list:
    table = asr_text.corrections_of(merged)
    return [{"target": str(t), "aliases": asr_text._aliases(v)} for t, v in table.items()]


def save_corrections(items, merged: dict, path: Path = None) -> list:
    """校验后把替换表整个写进 data/.config.yaml 的 asr_corrections。返回修改的字段。"""
    if not isinstance(items, list):
        raise ValueError("替换表格式不对")
    table, owner = {}, {}
    for item in items:
        target = re.sub(r"\s+", " ", str((item or {}).get("target") or "")).strip()
        aliases = [re.sub(r"\s+", " ", a) for a in asr_text._aliases((item or {}).get("aliases"))]
        if not target and not aliases:
            continue
        if not target:
            raise ValueError(f"“{aliases[0]}”要改成什么？请填写正确写法")
        if len(target) > 40:
            raise ValueError(f"正确写法“{target}”太长了，不超过 40 个字")
        clean = []
        for a in aliases:
            if len(a) < 2 or len(a) > 40:
                raise ValueError(f"认错的写法“{a}”需要 2～40 个字；太短容易误改普通的话")
            if a.lower() == target.lower():
                continue  # 和正确写法一样，不用替换
            other = owner.get(a.lower())
            if other and other != target:
                raise ValueError(f"“{a}”同时要改成“{other}”和“{target}”，只能保留一个")
            owner[a.lower()] = target
            if a.lower() not in {x.lower() for x in clean}:
                clean.append(a)
        if not clean:
            raise ValueError(f"“{target}”还没有认错的写法")
        table.setdefault(target, [])
        table[target] += [a for a in clean if a.lower() not in {x.lower() for x in table[target]}]
    if table == {t: list(v) for t, v in asr_text.corrections_of(merged).items()}:
        return []
    with config_store.edit_custom(path) as data:
        data["asr_corrections"] = CommentedMap((t, CommentedSeq(v)) for t, v in table.items())
    return ["asr_corrections"]


# ---------- 说完停顿时长（VAD） ----------
# 服务端 VAD 安静超过这么久就判定一句话说完，交给语音识别。
# 流式识别（type 以 _stream 结尾）由识别服务自己断句：豆包会用这个时长（end_window_size），其他的不起作用。
VAD_SILENCE_FIELD = "min_silence_duration_ms"
VAD_SILENCE_RANGE = (200, 5000)


def describe_vad(merged: dict) -> dict:
    name = _selected(merged, "VAD")
    entry = _entry(merged, "VAD", name)
    asr = _entry(merged, "ASR", _selected(merged, "ASR"))
    return {
        "selected": name,
        "supported": entry.get("type") == "silero",
        "silence_ms": int(entry.get(VAD_SILENCE_FIELD) or 1000),
        "range": list(VAD_SILENCE_RANGE),
        "asr_stream": str(asr.get("type") or "").endswith("_stream") and asr.get("type") != "doubao_stream",
        "asr_doubao": asr.get("type") == "doubao_stream",
    }


def save_vad_silence(raw, merged: dict, path: Path = None):
    """校验后写入当前 VAD 条目的停顿时长，返回写入的毫秒数；和原来一样时返回 None。"""
    name = _selected(merged, "VAD")
    entry = _entry(merged, "VAD", name)
    if entry.get("type") != "silero":
        raise ValueError("当前语音检测不是 SileroVAD，后台只支持设置 SileroVAD 的停顿时长")
    low, high = VAD_SILENCE_RANGE
    try:
        value = int(str(raw).strip())
        if not low <= value <= high:
            raise ValueError
    except ValueError:
        raise ValueError(f"停顿时长需要是 {low}～{high} 之间的整数（毫秒）")
    if value == entry.get(VAD_SILENCE_FIELD):
        return None
    with config_store.edit_custom(path) as data:
        data.setdefault("VAD", CommentedMap()).setdefault(name, CommentedMap())[VAD_SILENCE_FIELD] = value
    return value


# ---------- 语音合成 ----------

def describe_tts(merged: dict) -> dict:
    name = _selected(merged, "TTS")
    entry = _entry(merged, "TTS", name)
    return {
        "selected": name,
        "type": entry.get("type", name),
        "voice": str(entry.get("voice") or ""),
        "voices": catalog.EDGE_VOICES,
        **tts_settings.describe(merged),
    }


def save_tts_voice(voice: str, merged: dict, path: Path = None) -> list:
    name = _selected(merged, "TTS")
    if _entry(merged, "TTS", name).get("type") != "edge":
        raise ValueError("当前语音合成不是 EdgeTTS，后台只支持设置 EdgeTTS 的音色")
    if voice not in catalog.EDGE_VOICE_VALUES:
        raise ValueError("不支持这个音色")
    return config_store.update_entry("TTS", name, {"voice": voice}, merged, path)


# ---------- 意图识别 ----------

def plugin_configured(merged: dict, plugin: dict) -> bool:
    """插件需要的设置是否已填写（上游默认值是"你的…"或"mk-xxx"这类占位文本）。
    后台能设置的插件看必填字段，其余的看 plugins.<config>.api_key。"""
    if plugin.get("settings"):
        return plugin_settings.configured(merged, plugin["settings"])
    key = ((merged.get("plugins") or {}).get(plugin["config"]) or {}).get("api_key")
    return not config_store.is_placeholder(key) and not str(key).strip().lower().endswith("-xxx")


def describe_intent(merged: dict) -> dict:
    name = _selected(merged, "Intent")
    mode = config_store.entry_type(merged, "Intent", name)
    functions = [str(f) for f in _entry(merged, "Intent", "function_call").get("functions") or []]
    return {
        "selected": name,
        "mode": mode if mode in catalog.INTENT_MODES else "other",
        "plugins": [{**p, "enabled": p["name"] in functions,
                     "needs_config": bool(p.get("config")) and not plugin_configured(merged, p)}
                    for p in catalog.PLUGINS],
        "always_on": catalog.ALWAYS_ON_PLUGINS,
        "extra": [f for f in functions if f not in catalog.PLUGIN_NAMES],
    }


def intent_entry_name(merged: dict, mode: str) -> str:
    """意图识别模式对应的条目名（上游里条目名和类型同名，用户改过名时按类型找）。"""
    intents = merged.get("Intent") or {}
    if isinstance(intents.get(mode), dict) and intents[mode].get("type", mode) == mode:
        return mode
    for name, entry in intents.items():
        if isinstance(entry, dict) and entry.get("type") == mode:
            return name
    raise KeyError(mode)


def save_intent_plugins(functions: list, merged: dict, path: Path = None) -> list:
    """保存 function_call 的插件列表。后台不认识的插件（用户手动加的）原样保留。"""
    if not isinstance(functions, list):
        raise ValueError("插件列表格式不对")
    unknown = [f for f in functions if f not in catalog.PLUGIN_NAMES]
    if unknown:
        raise ValueError(f"不认识的插件：{', '.join(map(str, unknown))}")
    name = intent_entry_name(merged, "function_call")
    current = [str(f) for f in _entry(merged, "Intent", name).get("functions") or []]
    wanted = [p["name"] for p in catalog.PLUGINS if p["name"] in functions]
    wanted += [f for f in current if f not in catalog.PLUGIN_NAMES]
    if wanted == current:
        return []
    with config_store.edit_custom(path) as data:
        target = data.setdefault("Intent", CommentedMap()).setdefault(name, CommentedMap())
        target["functions"] = CommentedSeq(wanted)
    return ["functions"]


# ---------- 呼名 ----------

ALIAS_BAD = re.compile(r"[\s,，.。!！?？、~～;；:：\"'“”‘’]")


def call_targets(merged: dict) -> list:
    """可以配称呼的模型，同 model_choices。

    defaults 是添加这个模型时预填的称呼：API 厂商填英文厂商名；模型服务的模型和自定义没有固定名字，不预填。"""
    return [{"entry": c["entry"],
             "label": f"{c['label']}（API）" if c["entry"] in catalog.VENDOR_BY_ENTRY else c["label"],
             "defaults": [] if c["vendor"] == "custom" else [c["vendor"]]}
            for c in model_choices(merged)]


def describe_call_names(merged: dict) -> dict:
    from core.agent import call_names

    cfg = merged.get("call_names") or {}
    raw = cfg.get("models") or {}
    targets = {t["entry"]: t for t in call_targets(merged)}
    models = []
    for key, value in raw.items():
        entry = str(key)
        t = targets.get(entry)
        models.append({"entry": entry, "label": t["label"] if t else entry, "aliases": call_names._aliases(value),
                       "ready": t is not None})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "reply": str(cfg.get("reply") or call_names.DEFAULT_REPLY),
        "models": models,
        "targets": list(targets.values()),
        "default": _selected(merged, "LLM"),
        "prefixes": ["嗨", "嘿", "hi", "hey"],
    }


def _clean_aliases(raw, label: str, taken: dict) -> list:
    from core.agent import call_names

    if not isinstance(raw, list):
        raise ValueError("称呼格式不对")
    aliases = []
    for item in raw:
        alias = str(item or "").strip().lower()
        if not alias or alias in aliases:
            continue
        if ALIAS_BAD.search(alias):
            raise ValueError(f"称呼“{alias}”里不能有空格或标点")
        if not 2 <= len(alias) <= 20:
            raise ValueError(f"称呼“{alias}”要在 2 到 20 个字之间")
        if alias in call_names._PREFIXES:
            raise ValueError(f"“{alias}”是开头的招呼语，不能当称呼")
        if alias in taken:
            raise ValueError(f"称呼“{alias}”同时给了 {taken[alias]} 和 {label}，只能给一个模型")
        taken[alias] = label
        aliases.append(alias)
    if not aliases:
        raise ValueError(f"{label} 至少要有一个称呼，不用的话请删掉这个模型")
    return aliases


def save_call_names(payload: dict, merged: dict, path: Path = None) -> list:
    """保存呼名设置，返回修改了的项。取值不对时抛出 ValueError。

    payload["models"] 是 [{"entry": 条目名, "aliases": [...]}]。新加的模型必须已经配置好；
    原来就有、后来没配置好的模型可以保留（叫到时设备会提示还没配置好）。
    """
    from core.agent import call_names

    current = describe_call_names(merged)
    enabled = bool(payload.get("enabled", current["enabled"]))
    reply = str(payload.get("reply", current["reply"]) or "").strip() or call_names.DEFAULT_REPLY
    if len(reply) > 40:
        raise ValueError("回复太长了，请控制在 40 个字以内")
    raw = payload.get("models")
    if raw is None:
        raw = [{"entry": m["entry"], "aliases": m["aliases"]} for m in current["models"]]
    if not isinstance(raw, list):
        raise ValueError("模型列表格式不对")
    known = {m["entry"]: m["label"] for m in current["models"]}
    known.update({t["entry"]: t["label"] for t in current["targets"]})
    allowed = {t["entry"] for t in current["targets"]} | {m["entry"] for m in current["models"]}
    models, taken = {}, {}
    for item in raw:
        entry = str((item or {}).get("entry") or "") if isinstance(item, dict) else ""
        if entry not in allowed:
            raise ValueError(f"模型“{entry}”还没配置好，请先到“大模型”页填好再来")
        if entry in models:
            raise ValueError(f"{known[entry]} 重复了")
        models[entry] = _clean_aliases(item.get("aliases"), known[entry], taken)
    if enabled and not models:
        raise ValueError("至少要给一个模型配称呼")

    old_models = {m["entry"]: m["aliases"] for m in current["models"]}
    changed = [k for k, new, old in (("enabled", enabled, current["enabled"]), ("reply", reply, current["reply"]),
                                     ("models", list(models.items()), list(old_models.items()))) if new != old]
    if not changed:
        return []
    with config_store.edit_custom(path) as data:
        cfg = data.setdefault("call_names", CommentedMap())
        cfg["enabled"] = enabled
        cfg["reply"] = reply
        cfg["models"] = CommentedMap((k, CommentedSeq(v)) for k, v in models.items())
    return changed
