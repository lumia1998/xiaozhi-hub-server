"""管理后台的 HTTP 接口。

安全约定：
- 首次打开后台时设置密码，从哪台设备访问都可以（部署到云端服务器时也能直接设置）。
- 登录后用 HttpOnly + SameSite=Strict 的 cookie 保持会话；写操作只接受 JSON 请求。
- API Key 只写不读：接口永远不返回完整的 Key。

页面使用的接口（/admin/api/model-services、asr、tts、intent 等）只暴露后台要管理的内容，并做取值校验。

本机设备接口（/kiosk/*）给和网关装在同一台机器上的设备客户端用（例如带屏幕的语音终端），不用登录，
只接受本机直接发来的请求，见 _from_this_machine。
"""

import asyncio
import copy
import functools
import io
import json
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import web

from config.logger import setup_logging
from core.admin import auth, catalog, config_store, connections, pages, pipeline, plugin_settings, tts_settings
from core.admin import model_services
from core.agent import extensions
from core.admin.status import collect_status
from core.agent import chat_history, llm_router, task_service
from core.device_pairing import PairingError
from core.utils.modules_initialize import initialize_modules
from core.utils.textUtils import EMOJI_MAP

TAG = __name__

# 后台的同步操作（读写配置、测试连接）用自己的线程：默认线程池和语音识别等共用，
# 被卡住的操作（如连不上的测试连接）占满它时，保存要排队十几秒，识别也跟着变慢
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="admin")

logger = setup_logging()
COOKIE_NAME = "xz_admin"
STATIC_DIR = Path(__file__).parent / "static"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
EMOTION_EMOJI = {name: emoji for emoji, name in EMOJI_MAP.items()}
KIOSK_ASR_MAX_BYTES = 16000 * 2 * 10  # /kiosk/asr 最多 10 秒音频
KIOSK_TTS_MAX_CHARS = 200  # /kiosk/tts 一次最多合成这么多字（提醒的内容，一两句话）
KIOSK_TTS_RATE = 24000     # /kiosk/tts 返回的采样率：Edge TTS 原本就是 24 kHz
ADMIN_TASK_LIFETIME_SECONDS = 1800
ADMIN_TASK_BUDGET_TOKENS = 12000


def _wav(pcm: bytes, rate: int) -> bytes:
    """16 位单声道 PCM 加上 WAV 文件头。"""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


class AdminHandler:
    def __init__(self, config: dict, ws_server=None, config_path: Path = None):
        self.config = config
        self.ws_server = ws_server
        self.config_path = config_path
        self.sessions = auth.SessionStore()
        # 后台点了更新网关、网关重启后，接着用重启前的登录（文件在 data/ 下，和配置放在一起）
        data_dir = Path(config_path).parent if config_path else config_store.custom_config_path().parent
        self.sessions_handover = data_dir / ".admin-sessions.json"
        if self.sessions.restore(self.sessions_handover):
            logger.bind(tag=TAG).info("更新网关后沿用了更新前的后台登录")
        self._emotion_runs = {}  # session_id -> {"task", "index", "total", "emotion"}
        self._voice_samples = {}  # 音色 -> mp3 字节（试听缓存）
        self._tts_preview_slots = asyncio.Semaphore(2)
        self.logger = logger
        self._model_management_lock = asyncio.Lock()
        self._mcp_probe_slots = asyncio.Semaphore(2)
        self.extensions_path = data_dir / 'llm_extensions.json'

    def register(self, app: web.Application) -> None:
        app.add_routes(
            [
                web.get("/admin", self.handle_index),
                web.get("/admin/", self.handle_index),
                web.get('/admin/assets/{name}', self.handle_asset),
                web.get("/admin/api/state", self.handle_state),
                web.post("/admin/api/setup", self.handle_setup),
                web.post("/admin/api/login", self.handle_login),
                web.post("/admin/api/logout", self.handle_logout),
                web.get("/admin/api/status", self.handle_status),
                web.get("/admin/api/devices", self.handle_devices),
                web.get("/admin/api/memory", self.handle_memory),
                web.post("/admin/api/memory/save", self.handle_memory_save),
                web.post("/admin/api/memory/owner", self.handle_memory_owner),
                web.post("/admin/api/memory/preferences/save", self.handle_memory_preference_save),
                web.post("/admin/api/memory/preferences/delete", self.handle_memory_preference_delete),
                web.get("/admin/api/tasks", self.handle_tasks),
                web.post("/admin/api/tasks/start", self.handle_task_start),
                web.post("/admin/api/tasks/cancel", self.handle_task_cancel),
                web.post("/admin/api/tasks/answer", self.handle_task_answer),
                web.get("/admin/api/pairing", self.handle_pairing),
                web.post("/admin/api/pairing/issue", self.handle_pairing_issue),
                web.post("/admin/api/pairing/revoke", self.handle_pairing_revoke),
                web.post("/admin/api/pairing/enabled", self.handle_pairing_enabled),
                web.post("/admin/api/devices/{session_id}/emotion", self.handle_emotion_one),
                web.post("/admin/api/devices/{session_id}/emotion-test", self.handle_emotion_test),
                web.post("/admin/api/devices/{session_id}/emotion-test/stop", self.handle_emotion_stop),
                web.get('/admin/api/model-services', self.handle_model_services),
                web.post('/admin/api/model-services/discover', self.handle_models_discover),
                web.post('/admin/api/model-services/probe', self.handle_models_probe),
                web.post('/admin/api/model-services/use', self.handle_models_use),
                web.post('/admin/api/persona', self.handle_persona),
                web.get('/admin/api/extensions', self.handle_extensions),
                web.post('/admin/api/extensions', self.handle_extensions_save),
                web.post('/admin/api/extensions/probe', self.handle_extensions_probe),
                web.get("/admin/api/pipeline", self.handle_pipeline),
                web.get("/admin/api/asr", self.handle_asr),
                web.post("/admin/api/vad", self.handle_vad_save),
                web.post("/admin/api/asr/use", self.handle_asr_use),
                web.post("/admin/api/asr/doubao", self.handle_doubao_save),
                web.post("/admin/api/asr/doubao/test", self.handle_doubao_test),
                web.post("/admin/api/asr/hotwords", self.handle_hotwords_save),
                web.post("/admin/api/asr/corrections", self.handle_corrections_save),
                web.get("/admin/api/tts", self.handle_tts),
                web.post("/admin/api/tts", self.handle_tts_save),
                web.get("/admin/api/tts/preview", self.handle_tts_preview),
                web.post("/admin/api/tts/preview", self.handle_tts_provider_preview),
                web.get("/admin/api/intent", self.handle_intent),
                web.post("/admin/api/intent", self.handle_intent_save),
                web.get("/admin/api/plugin-settings/{id}", self.handle_plugin_settings),
                web.post("/admin/api/plugin-settings/{id}", self.handle_plugin_settings_save),
                web.post("/admin/api/plugin-settings/{id}/test", self.handle_plugin_settings_test),
                web.get("/admin/api/call-names", self.handle_call_names),
                web.post("/admin/api/call-names", self.handle_call_names_save),
                web.post("/admin/api/call-names/learn", self.handle_call_names_learn),
                web.post("/admin/api/call-names/learn/stop", self.handle_call_names_learn_stop),
                web.get("/kiosk/llm", self.handle_kiosk_llm),
                web.post("/kiosk/llm/use", self.handle_kiosk_llm_use),
                web.post("/kiosk/asr", self.handle_kiosk_asr),
                web.post("/kiosk/tts", self.handle_kiosk_tts),
                web.get("/kiosk/chat/history", self.handle_kiosk_chat_history),
                web.post("/kiosk/chat/clear", self.handle_kiosk_chat_clear),
            ]
        )

    # ---------- 工具方法 ----------

    @staticmethod
    def _json(data, status=200):
        return web.json_response(data, status=status, headers=SECURITY_HEADERS)

    def _error(self, message, status):
        return self._json({"ok": False, "error": message}, status)

    @staticmethod
    async def _blocking(func, *args, **kwargs):
        """在后台自己的线程里执行同步操作（配置文件读写、密码哈希），不阻塞负责音频下发的主事件循环。"""
        return await asyncio.get_running_loop().run_in_executor(_POOL, functools.partial(func, *args, **kwargs))

    def _logged_in(self, request: web.Request) -> bool:
        return self.sessions.valid(request.cookies.get(COOKIE_NAME))

    @staticmethod
    async def _read_json(request: web.Request):
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="需要 application/json")
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="JSON 格式错误")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="JSON 格式错误")
        return body

    def _login_response(self, request: web.Request, data: dict):
        response = self._json(data)
        response.set_cookie(
            COOKIE_NAME,
            self.sessions.create(),
            httponly=True,
            samesite="Strict",
            path="/admin",
            max_age=self.sessions.ttl,
        )
        return response

    def _require_login(self, request: web.Request):
        if not self._logged_in(request):
            raise web.HTTPUnauthorized(
                text='{"ok": false, "error": "未登录"}', content_type="application/json"
            )

    # ---------- 页面与登录 ----------

    async def handle_index(self, request: web.Request):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", headers=SECURITY_HEADERS)

    async def handle_asset(self, request: web.Request):
        name = request.match_info['name']
        if name not in ('tts-page.js', 'tts-page.css', 'models-page.js', 'models-page.css',
                        'extensions-page.js', 'memory-page.js', 'task-page.js', 'admin-shell.js', 'admin-theme.css', 'admin-icons.svg', 'radix-colors.css'):
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC_DIR / name, headers=SECURITY_HEADERS)

    async def handle_state(self, request: web.Request):
        return self._json(
            {
                "needs_setup": not config_store.get_password_hash(self.config_path),
                "logged_in": self._logged_in(request),
            }
        )

    async def handle_setup(self, request: web.Request):
        if config_store.get_password_hash(self.config_path):
            return self._error("密码已设置，请直接登录", 409)
        body = await self._read_json(request)
        password = str(body.get("password") or "")
        if len(password) < auth.MIN_PASSWORD_LENGTH:
            return self._error(f"密码至少 {auth.MIN_PASSWORD_LENGTH} 位", 400)
        password_hash = await self._blocking(auth.hash_password, password)
        await self._blocking(config_store.set_password_hash, password_hash, self.config_path)
        self.logger.bind(tag=TAG).info("管理后台密码已设置")
        return self._login_response(request, {"ok": True})

    async def handle_login(self, request: web.Request):
        body = await self._read_json(request)
        stored = await self._blocking(config_store.get_password_hash, self.config_path)
        if not stored:
            return self._error("尚未设置密码", 409)
        if not await self._blocking(auth.verify_password, str(body.get("password") or ""), stored):
            await asyncio.sleep(1)  # 减缓暴力尝试
            self.logger.bind(tag=TAG).warning(f"管理后台登录失败，来源 {request.remote}")
            return self._error("密码错误", 401)
        return self._login_response(request, {"ok": True})

    async def handle_logout(self, request: web.Request):
        self.sessions.revoke(request.cookies.get(COOKIE_NAME))
        response = self._json({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/admin")
        return response

    # ---------- 配置读取、切换与热加载 ----------

    def _merged(self) -> dict:
        if self.config_path:
            return config_store.load_merged(custom_path=self.config_path)
        return config_store.load_merged()

    def _running(self, section: str):
        return (self.config.get("selected_module") or {}).get(section)

    async def _update_task_worker(self):
        """Refresh the worker's enabled state after a committed LLM config change."""
        worker = getattr(self.ws_server, "task_worker", None) if self.ws_server is not None else None
        if worker is not None:
            try:
                await self._blocking(worker.update_config, self.config)
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"后台任务 worker 配置更新失败: {type(e).__name__}")

    async def _apply_if_selected(self, section: str, name: str, changed: list) -> dict:
        """保存条目后：改的是正在使用的条目才热加载，否则只保存。

        不是默认的条目也要同步进运行中的配置（同 /kiosk/llm/use）：呼名切模型照运行中的配置判断是否配好、
        创建实例，不同步的话新填好 Key 的厂商叫不动，改过的设置要重启才生效。"""
        if not changed:
            return {"applied": True, "note": "没有修改"}
        merged = await self._blocking(self._merged)
        if (merged.get("selected_module") or {}).get(section) != name:
            if name in (merged.get(section) or {}):
                self.config.setdefault(section, {})[name] = copy.deepcopy(merged[section][name])
                if section == "LLM":
                    await self._update_task_worker()
            return {"applied": True, "note": "已保存"}
        return await self._apply(section)

    async def _switch(self, section: str, name: str) -> dict:
        """切换模块：先写配置再热加载；加载失败时把配置恢复成原来的选择，避免下次启动出问题。

        返回 {"ok": bool, "applied": bool, "note": str}。
        """
        merged = await self._blocking(self._merged)
        if name not in (merged.get(section) or {}):
            raise KeyError(name)
        old = (merged.get("selected_module") or {}).get(section)
        if old == name and self._running(section) == name and \
                (self.config.get(section) or {}).get(name) == merged[section][name]:
            return {"ok": True, "applied": True, "note": "已经在使用"}
        await self._blocking(config_store.select_entry, section, name, merged, self.config_path)
        result = await self._apply(section)
        if result.get("failed"):
            if old and old != name:
                await self._blocking(config_store.select_entry, section, old, merged, self.config_path)
            result["note"] = f"切换失败：{result['error']}。已恢复为原来的 {old or '配置'}"
            self.logger.bind(tag=TAG).warning(f"管理后台切换 {section} 为 {name} 失败，已恢复为 {old}")
            return {"ok": False, **result}
        self.logger.bind(tag=TAG).info(f"管理后台切换 {section} 为: {name}")
        return {"ok": True, **result}

    async def _apply(self, section: str) -> dict:
        """把磁盘上的配置热加载到运行中的服务。

        直接修改共享的 config 字典，再按模块重建实例：
        - LLM、Intent：立即重建；
        - ASR：后台重新加载（本地模型需要几秒）；
        - TTS：每个连接各自创建，不用重建。
        已建立的设备连接继续使用旧实例，设备重新连接后才会用上新配置。
        """
        merged = await self._blocking(self._merged)
        new_section = merged.get(section, {})
        new_selected = merged["selected_module"][section]

        def commit():
            self.config[section] = new_section
            self.config.setdefault("selected_module", {})[section] = new_selected

        if section == "TTS" or self.ws_server is None:
            commit()
            if section == "LLM":
                await self._update_task_worker()
            if section == "TTS":
                return {"applied": True, "note": "已保存，设备重新连接后生效"}
            return {"applied": False, "note": "已保存，重启服务后生效"}
        flags = {"LLM": "init_llm", "Intent": "init_intent", "ASR": "init_asr"}
        attr = {"LLM": "_llm", "Intent": "_intent", "ASR": "_asr"}[section]
        key = {"LLM": "llm", "Intent": "intent", "ASR": "asr"}[section]
        # 先用"新配置"创建实例；成功后才写入运行中的配置并替换实例，失败时服务保持原来的状态
        candidate = {**self.config, section: new_section,
                     "selected_module": {**self.config.get("selected_module", {}), section: new_selected}}
        try:
            async with self.ws_server.config_lock:
                modules = await self._blocking(initialize_modules, self.logger, candidate, **{flags[section]: True})
                commit()
                setattr(self.ws_server, attr, modules[key])
                if section == "LLM":
                    await self._update_task_worker()
            return {"applied": True, "note": "已生效，设备重新连接后使用新配置"}
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"热加载 {section} 失败: {e}")
            error = str(e).strip().rstrip("。.") or type(e).__name__
            return {"applied": False, "failed": True, "error": error,
                    "note": f"已保存，但加载失败：{error}。服务仍在使用原来的配置，请检查后重试"}

    # ---------- 呼名 ----------

    async def handle_call_names(self, request: web.Request):
        self._require_login(request)
        from core.agent.call_names import learner

        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **pages.describe_call_names(merged), "learning": learner.status(),
                           "devices": connections.snapshot()})

    async def handle_call_names_save(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            changed = await self._blocking(pages.save_call_names, body, merged, self.config_path)
        except ValueError as e:
            return self._error(str(e), 400)
        note = "已保存，马上生效" if changed else "没有修改"
        if changed:
            new_merged = await self._blocking(self._merged)
            self._push_config("call_names", new_merged.get("call_names") or {})
            self.logger.bind(tag=TAG).info(f"管理后台更新了呼名设置: {', '.join(changed)}")
            # 呼名里模型的名字会自动并入热词，模型或开关变了要重新加载语音识别（只改回复语不用）
            if {"models", "enabled"} & set(changed) and pages.describe_hotwords(new_merged)["supported"] \
                    and self._running("ASR") == \
                    (new_merged.get("selected_module") or {}).get("ASR"):
                result = await self._apply("ASR")
                if result.get("failed"):
                    note += f"；热词重新加载失败：{result['error']}"
        return self._json({"ok": True, "changed": changed, "note": note})

    def _push_config(self, key: str, value) -> None:
        """设置马上生效：服务的配置和每个在线连接各自的配置副本都要更新。"""
        self.config[key] = copy.deepcopy(value)
        for item in connections.snapshot():
            conn = connections.get(item["session_id"])
            if conn is not None:
                conn.config[key] = copy.deepcopy(value)

    async def handle_call_names_learn(self, request: web.Request):
        self._require_login(request)
        from core.agent.call_names import learner

        body = await self._read_json(request)
        session_id = str(body.get("session_id") or "") or None
        online = [d["session_id"] for d in connections.snapshot()]
        if not online:
            return self._error("没有设备在线，请先唤醒设备", 400)
        if session_id and session_id not in online:
            return self._error("这台设备已经离线", 400)
        target = str(body.get("target") or "") or None
        learner.start(session_id or (online[0] if len(online) == 1 else None), target=target)
        self.logger.bind(tag=TAG).info("管理后台开始录入呼名")
        return self._json({"ok": True, "learning": learner.status()})

    async def handle_call_names_learn_stop(self, request: web.Request):
        self._require_login(request)
        from core.agent.call_names import learner

        learner.stop()
        return self._json({"ok": True, "learning": learner.status()})

    # ---------- 状态 ----------

    async def handle_status(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        status = await self._blocking(collect_status, self.config, merged)
        return self._json({"ok": True, **status})

    # ---------- 更新网关 ----------

    async def handle_model_services(self, request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        return self._json({'ok': True, **model_services.describe(merged)})

    async def handle_models_discover(self, request):
        self._require_login(request)
        body = await self._read_json(request)
        if self._model_management_lock.locked(): return self._error('模型设置正在处理，请稍后重试', 409)
        async with self._model_management_lock:
            try:
                merged = await self._blocking(self._merged)
                sid, service, old = model_services.normalized_service(body, merged)
                report = await model_services.discover(service)
                if not report['models']:
                    return self._json({'ok': False, 'error': '未获取到模型，请检查接口地址和密钥', 'discovery': report['discovery']}, 400)
                await self._blocking(model_services.save_discovered, sid, service, report, merged, self.config_path)
                fresh = await self._blocking(self._merged)
                self.config['LLM'] = fresh['LLM']; self.config['model_services'] = fresh['model_services']
                # A refreshed service can change credentials of a currently selected model.
                active = self._running('LLM')
                if (fresh['LLM'].get(active) or {}).get('service_id') == sid:
                    result = await self._apply('LLM')
                    if result.get('failed'): return self._error(result['note'], 400)
                else:
                    await self._update_task_worker()
                return self._json({'ok': True, 'id': sid, **model_services.describe(fresh)})
            except (ValueError, TypeError) as e:
                return self._error(str(e), 400)

    async def _model_probe(self, body):
        merged = await self._blocking(self._merged)
        sid, model = body.get('id'), body.get('model')
        service = (merged.get('model_services') or {}).get(sid)
        if not service or not isinstance(model, str) or model not in {m['id'] for m in service.get('models', [])}:
            raise ValueError('请先获取模型列表，再选择模型')
        report = await self._blocking(model_services.probe_sync, service['base_url'], service.get('api_key', ''), model)
        await self._blocking(model_services.save_probe, sid, model, report, merged, self.config_path)
        fresh = await self._blocking(self._merged)
        self.config['LLM'] = fresh['LLM']; self.config['model_services'] = fresh['model_services']
        await self._update_task_worker()
        return model_services.model_entry(service, sid, model), report

    async def handle_models_probe(self, request):
        self._require_login(request)
        body = await self._read_json(request)
        if self._model_management_lock.locked(): return self._error('模型设置正在处理，请稍后重试', 409)
        async with self._model_management_lock:
            try: name, report = await self._model_probe(body)
            except (ValueError, TypeError) as e: return self._error(str(e), 400)
        return self._json({'ok': True, 'entry': name, 'probe': report})

    async def handle_models_use(self, request):
        self._require_login(request)
        body = await self._read_json(request)
        if self._model_management_lock.locked(): return self._error('模型设置正在处理，请稍后重试', 409)
        async with self._model_management_lock:
            try:
                merged = await self._blocking(self._merged)
                name = body.get('entry')
                if body.get('id'):
                    service = (merged.get('model_services') or {}).get(body['id']) or {}
                    row = next((m for m in service.get('models', []) if m['id'] == body.get('model')), None)
                    if row is None:raise ValueError('模型不在已保存的列表中')
                    name = model_services.model_entry(service, body['id'], body['model'])
                    merged = await self._blocking(self._merged)
                entry = (merged.get('LLM') or {}).get(name) or {}
                if entry.get('type') not in ('openai', 'multi_api', 'pi') or not llm_router.model_ready(merged, name):
                    raise ValueError('模型没有配置好或不可用')
                self.config['LLM'] = merged['LLM']
                if self._running('LLM') == name:
                    result = {'ok': True, **await self._apply('LLM')}
                    if result.get('failed'): result['ok'] = False
                else:
                    result = await self._switch('LLM', name)
                if result.get('ok'):
                    await self._update_task_worker()
                return self._json(result)
            except (ValueError, TypeError, KeyError) as e: return self._error(str(e), 400)

    async def handle_persona(self, request):
        self._require_login(request)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            text = await self._blocking(model_services.save_persona, body.get('persona'), body.get('expected'), merged, self.config_path)
        except (ValueError, TypeError) as e: return self._error(str(e), 400)
        self.config['prompt'] = text
        return self._json({'ok': True, 'persona': text, 'note': '人设已保存，下次对话连接时生效'})

    async def handle_extensions(self, request):
        self._require_login(request)
        return self._json({'ok': True, **await self._blocking(extensions.describe, self.extensions_path)})

    async def handle_extensions_save(self, request):
        self._require_login(request)
        body = await self._read_json(request)
        try:
            data = await self._blocking(extensions.save, body, self.extensions_path)
            return self._json({'ok': True, **data, 'note': '已保存。MCP 在下次对话连接时生效，Skill 在下次提问时生效。'})
        except (ValueError, TypeError) as e:
            return self._error(str(e), 400)

    async def handle_extensions_probe(self, request):
        self._require_login(request)
        body = await self._read_json(request)
        if self._mcp_probe_slots.locked():
            return self._error('已有连接测试正在进行，请稍后重试', 429)
        try:
            async with self._mcp_probe_slots:
                result = await extensions.probe(body, self.extensions_path)
            return self._json(result, 200 if result['ok'] else 400)
        except (ValueError, TypeError) as e:
            return self._error(str(e), 400)

    # ---------- 本机设备接口 ----------

    @staticmethod
    def _from_this_machine(request: web.Request) -> bool:
        """请求直接来自本机。经过反向代理转发来的（带 X-Forwarded-For / Forwarded）不算：
        代理和网关在同一台机器上时来源地址也是 127.0.0.1，但发起请求的可能是任何人。"""
        if "X-Forwarded-For" in request.headers or "Forwarded" in request.headers:
            return False
        return request.remote in ("127.0.0.1", "::1")

    async def handle_kiosk_llm(self, request: web.Request):
        """可选的模型（配好的 API 厂商和模型服务里的模型）和当前选中的。

        带 ?device=<device-id> 时，selected 是这台设备实际会用的模型：它上次切到的（呼名或在这里切换），
        还可用的话；default 始终是后台默认模型。
        """
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **await self._kiosk_models(merged, request.query.get("device"))})

    async def _kiosk_models(self, merged: dict, device: str = None) -> dict:
        options = [{"kind": "api", **c} for c in pages.model_choices(merged)]
        default = (merged.get("selected_module") or {}).get("LLM")
        selected = default
        if device:
            name = (llm_router.store().get(device) or {}).get("model")
            if name and any(o["entry"] == name for o in options):
                selected = name
        return {"selected": selected, "default": default, "options": options}

    async def handle_kiosk_llm_use(self, request: web.Request):
        """把这台设备切到 body 指定的模型（{"entry": …}，或只给 {"vendor": …}；都带 "device"），同呼名切换：
        只这台设备切过去并记住，在线的连接马上跟着切。后台默认模型不变，不写配置、不重建默认实例；
        默认模型只在管理后台改。

        返回切换后的模型列表（同 GET /kiosk/llm?device=…），设备不用再查一次。
        """
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body = await self._read_json(request)
        device = str(body.get("device") or "")
        if not device:
            return self._error("缺少 device", 400)
        merged = await self._blocking(self._merged)
        name = str(body.get("entry") or "")
        if not name:
            vendor = catalog.VENDOR_BY_ID.get(str(body.get("vendor") or ""))
            if vendor is None:
                return self._error("不支持这个厂商", 404)
            name = vendor["entry"]
        if not any(c["entry"] == name for c in pages.model_choices(merged)):
            return self._error(f"{llm_router.model_label(name)} 还没配置好", 400)
        # 后台保存过、但没设成默认的条目，运行中的配置里还是旧的（或没有）：补进去，切换时照它创建实例
        running = self.config.setdefault("LLM", {})
        if running.get(name) != merged["LLM"][name]:
            running[name] = copy.deepcopy(merged["LLM"][name])
        before = llm_router.store().get(device)
        await self._blocking(llm_router.store().put, device, {"model": name})
        for item in connections.snapshot():
            conn = connections.get(item["session_id"])
            if conn is None or item["device_id"] != device:
                continue
            try:
                ok = await self._blocking(llm_router.switch, conn, name, remember=False)
                error = "" if ok is not False else "模型没有配置好"
            except Exception as e:
                ok, error = False, str(e).strip() or type(e).__name__
            if ok is False:
                self.logger.bind(tag=TAG).error(f"在线连接 {item['session_id']} 切换模型 {name} 失败: {error}")
                if before:   # 恢复成原来记住的
                    await self._blocking(llm_router.store().put, device,
                                         {k: v for k, v in before.items() if k != "last_active"})
                else:
                    await self._blocking(llm_router.store().forget, device)
                return self._error(f"切换失败：{error}", 500)
        self.logger.bind(tag=TAG).info(f"设备 {device} 切换模型为 {name}（只这台设备）")
        return self._json({"ok": True, "applied": True, "note": "已切换，这台设备之后都用它",
                           **await self._kiosk_models(merged, device)})

    async def handle_kiosk_asr(self, request: web.Request):
        """只识别、不对话：body 是 10 秒以内的 16kHz 单声道 16 位 PCM，返回 {"text", "ms"}。

        用服务已经加载、所有连接共用的本机识别模型（设备客户端用它复核唤醒词）。
        当前识别不是本机模型（如豆包流式）时返回 501，客户端据此跳过复核。
        """
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        asr = getattr(self.ws_server, "_asr", None)
        if not callable(getattr(asr, "recognize_pcm", None)):
            return self._error("当前语音识别不是本机模型", 501)
        pcm = await request.read()
        if len(pcm) < 2 or len(pcm) > KIOSK_ASR_MAX_BYTES:
            return self._error("要 10 秒以内的 16kHz 单声道 16 位 PCM", 400)
        started = time.monotonic()
        text = await self._blocking(asr.recognize_pcm, pcm)
        return self._json({"ok": True, "text": text, "ms": round((time.monotonic() - started) * 1000)})

    async def handle_kiosk_tts(self, request: web.Request):
        """{"text"}：只合成、不对话，返回 16 位单声道 WAV（24 kHz）。

        用运行中配置选的语音合成和音色（和设备听到的回复一样）。设备客户端设提醒时用它把要念的话预先合成好
        存在本机，到点直接播，不再依赖网关。流式合成（没有整句合成）返回 501，客户端到点只响铃、屏幕显示文字。"""
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body = await self._read_json(request)
        text = str(body.get("text") or "").strip()
        if not text or len(text) > KIOSK_TTS_MAX_CHARS:
            return self._error(f"text 要 1 到 {KIOSK_TTS_MAX_CHARS} 个字", 400)
        started = time.monotonic()
        try:
            pcm = await self._blocking(self._synthesize, text)
        except NotImplementedError:
            return self._error("当前语音合成不支持整句合成", 501)
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"/kiosk/tts 合成失败: {e}")
            pcm = None
        if not pcm:
            return self._error("合成失败，请检查语音合成服务能否访问", 502)
        self.logger.bind(tag=TAG).info(f"/kiosk/tts 合成 {len(text)} 字，用时 {time.monotonic() - started:.1f}s")
        return web.Response(body=_wav(pcm, KIOSK_TTS_RATE), content_type="audio/wav", headers=SECURITY_HEADERS)

    def _synthesize(self, text: str) -> bytes:
        from core.providers.tts.dto.dto import InterfaceType
        from core.utils.modules_initialize import initialize_tts

        tts = initialize_tts(self.config)
        if tts.interface_type != InterfaceType.NON_STREAM:
            raise NotImplementedError
        return tts.synthesize_pcm(text, KIOSK_TTS_RATE)

    async def handle_kiosk_chat_history(self, request: web.Request):
        """?device=<device-id>[&limit=N]：这台设备存下来的聊天记录，给屏幕显示。

        返回 {"models": {条目名: [{id, role: user|bot, text, time, interrupted}]}}，每个模型最近 limit 条
        （默认 chat_history.display_limit）。"""
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        device = request.query.get("device") or ""
        if not device:
            return self._error("缺少 device", 400)
        cfg = chat_history.settings(await self._blocking(self._merged))
        try:
            limit = max(1, min(int(request.query.get("limit") or cfg["display_limit"]), 500))
        except ValueError:
            return self._error("limit 要是数字", 400)
        models = await self._blocking(chat_history.history, device, limit)
        return self._json({"ok": True, "models": models})

    async def handle_kiosk_chat_clear(self, request: web.Request):
        """{"device", "memory_only"?}：清除这台设备的聊天记录（所有模型），和语音"清除聊天记录"同一套。

        清库里的记录和记忆、在线连接里的对话。memory_only：只清记忆，记录留着。返回 {"deleted"}。"""
        if not self._from_this_machine(request):
            return self._error("仅限本机", 403)
        body = await self._read_json(request)
        device = str(body.get("device") or "")
        if not device:
            return self._error("缺少 device", 400)
        online = [c for c in (connections.get(i["session_id"]) for i in connections.snapshot()
                              if i["device_id"] == device) if c is not None]
        result = await self._blocking(chat_history.clear, device, bool(body.get("memory_only")), online)
        return self._json({"ok": True, **result})

    # ---------- 工作原理 ----------

    async def handle_pipeline(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **await self._blocking(pipeline.describe, merged)})

    # ---------- 语音识别、语音合成 ----------

    async def handle_asr(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **await self._blocking(pages.describe_asr, merged),
                           "running": self._running("ASR")})

    async def handle_asr_use(self, request: web.Request):
        """切换到本地 X-ASR 或豆包。"""
        self._require_login(request)
        body = await self._read_json(request)
        choice = str(body.get("choice") or "")
        merged = await self._blocking(self._merged)
        d = await self._blocking(pages.describe_asr, merged)
        if choice == "local":
            name = catalog.LOCAL_ASR_ENTRY
            if not d["local"]["exists"]:
                return self._error(f"配置里没有 {name} 条目，请重新运行安装脚本生成配置", 404)
            if not (d["local"]["model"] or {}).get("ok"):
                return self._error(f"X-ASR 模型文件不完整（{(d['local']['model'] or {}).get('detail', '')}），"
                                   "请重新运行安装脚本下载", 400)
        elif choice == "doubao":
            name = catalog.DOUBAO_ASR_ENTRY
            if not d["doubao"]["configured"]:
                return self._error("豆包还没配置好，请先填写 API Key（或 APP ID 和 Access Token）并保存", 400)
        else:
            return self._error("只能选本地 X-ASR 或豆包", 400)
        try:
            result = await self._switch("ASR", name)
        except KeyError:
            return self._error(f"配置里没有 {name}", 404)
        return self._json(result) if result["ok"] else self._error(result["note"], 400)

    async def handle_doubao_save(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            changed = await self._blocking(pages.save_doubao, body, merged, self.config_path)
        except ValueError as e:
            return self._error(str(e), 400)
        self.logger.bind(tag=TAG).info(f"管理后台更新了豆包语音识别设置: {', '.join(changed) or '无变化'}")
        return self._json({"ok": True, "changed": changed,
                           **await self._apply_if_selected("ASR", catalog.DOUBAO_ASR_ENTRY, changed)})

    async def handle_doubao_test(self, request: web.Request):
        self._require_login(request)
        from core.providers.asr.doubao_stream import check_connection

        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            config = pages.doubao_test_config(body, merged)
        except ValueError as e:
            return self._error(str(e), 400)
        result = await check_connection(config)
        return self._json(result) if result["ok"] else self._error(result["error"], 400)

    async def handle_hotwords_save(self, request: web.Request):
        """保存热词后重新加载语音识别（X-ASR 约 1 秒，豆包马上）：新连接马上用上，在线设备重连后生效。"""
        self._require_login(request)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            changed = await self._blocking(pages.save_hotwords, body.get("words"), merged, self.config_path)
        except ValueError as e:
            return self._error(str(e), 400)
        if not changed:
            return self._json({"ok": True, "changed": [], "note": "没有修改"})
        self.logger.bind(tag=TAG).info("管理后台更新了热词")
        # 热词在配置顶层，先放进运行中的配置，重新加载语音识别时才用得上
        self._push_config("asr_hotwords", (await self._blocking(self._merged)).get("asr_hotwords") or [])
        name = merged["selected_module"]["ASR"]
        result = await self._apply_if_selected("ASR", name, changed)
        if result.get("failed"):
            return self._json({"ok": True, "changed": changed, **result})
        note = "已保存并重新加载语音识别；新连接马上生效，在线设备重新连接后生效" if result.get("applied") else result["note"]
        return self._json({"ok": True, "changed": changed, "applied": result.get("applied"), "note": note})

    async def handle_corrections_save(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            changed = await self._blocking(pages.save_corrections, body.get("items"), merged, self.config_path)
        except ValueError as e:
            return self._error(str(e), 400)
        if changed:
            self._push_config("asr_corrections", (await self._blocking(self._merged)).get("asr_corrections") or {})
            self.logger.bind(tag=TAG).info("管理后台更新了识别纠错表")
        return self._json({"ok": True, "changed": changed, "note": "已保存，马上生效" if changed else "没有修改"})

    async def handle_vad_save(self, request: web.Request):
        """保存说完停顿时长。VAD 实例是所有连接共用的，直接改它的阈值，在线设备的下一句话就用上。"""
        self._require_login(request)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            value = await self._blocking(pages.save_vad_silence, body.get("silence_ms"), merged, self.config_path)
        except ValueError as e:
            return self._error(str(e), 400)
        if value is None:
            return self._json({"ok": True, "changed": [], "note": "没有修改"})
        name = merged["selected_module"]["VAD"]
        self.logger.bind(tag=TAG).info(f"管理后台更新了说完停顿时长: {value}ms")
        if self._running("VAD") != name:
            return self._json({"ok": True, "changed": ["silence_ms"], "note": "已保存，重启服务后生效"})
        self.config.setdefault("VAD", {}).setdefault(name, {})[pages.VAD_SILENCE_FIELD] = value
        instances = [getattr(self.ws_server, "_vad", None)]
        for item in connections.snapshot():
            conn = connections.get(item["session_id"])
            if conn is None:
                continue
            instances.append(getattr(conn, "vad", None))
            vad_cfg = ((getattr(conn, "config", None) or {}).get("VAD") or {}).get(name)
            if isinstance(vad_cfg, dict):
                vad_cfg[pages.VAD_SILENCE_FIELD] = value
        for vad in instances:
            if hasattr(vad, "silence_threshold_ms"):
                vad.silence_threshold_ms = value
        # 豆包每句话开始时才把判停时长发给识别服务，改正在用的实例，下一句话就用上
        for item in connections.snapshot():
            asr = getattr(connections.get(item["session_id"]), "asr", None)
            if getattr(asr, "interface_type", None) is not None and hasattr(asr, "end_window_size"):
                asr.end_window_size = max(200, value)
        return self._json({"ok": True, "changed": ["silence_ms"], "note": "已保存，马上生效"})

    async def handle_tts(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **pages.describe_tts(merged), "running": self._running("TTS")})

    async def handle_tts_save(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        if 'provider' in body:
            try:
                merged = await self._blocking(self._merged)
                name, changed = await self._blocking(tts_settings.save, body['provider'], body.get('values', {}), merged, self.config_path)
            except (ValueError, TypeError) as e:
                return self._error(str(e), 400)
            fresh = await self._blocking(self._merged)
            self.config['TTS'] = fresh['TTS']
            self.config.setdefault('selected_module', {})['TTS'] = name
            self.logger.bind(tag=TAG).info(f'管理后台切换语音服务: {name}')
            return self._json({'ok': True, 'changed': changed, 'note': '已保存，设备下一次连接时生效'})
        try:
            merged = await self._blocking(self._merged)
            changed = await self._blocking(pages.save_tts_voice, str(body.get("voice") or ""), merged, self.config_path)
        except ValueError as e:
            return self._error(str(e), 400)
        name = (merged.get("selected_module") or {}).get("TTS")
        self.logger.bind(tag=TAG).info(f"管理后台更新了音色: {body.get('voice')}")
        return self._json({"ok": True, "changed": changed, **await self._apply_if_selected("TTS", name, changed)})

    async def handle_tts_preview(self, request: web.Request):
        """用 EdgeTTS 合成一句示例，返回 mp3 给浏览器播放。"""
        self._require_login(request)
        voice = request.query.get("voice", "")
        info = next((v for v in catalog.EDGE_VOICES if v["value"] == voice), None)
        if info is None:
            return self._error("不支持这个音色", 400)
        audio = self._voice_samples.get(voice)
        if audio is None:
            import edge_tts

            chunks = []
            try:
                async def synth():
                    async for chunk in edge_tts.Communicate(catalog.voice_sample(info["group"]), voice).stream():
                        if chunk["type"] == "audio":
                            chunks.append(chunk["data"])
                await asyncio.wait_for(synth(), timeout=20)
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"试听合成失败 {voice}: {e}")
                return self._error("试听合成失败，请检查电脑能否访问微软的语音服务", 502)
            audio = b"".join(chunks)
            self._voice_samples[voice] = audio
        return web.Response(body=audio, content_type="audio/mpeg",
                            headers={**SECURITY_HEADERS, "Cache-Control": "private, max-age=86400"})

    async def handle_tts_provider_preview(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        text = body.get('text', tts_settings.SAMPLE)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 200:
            return self._error('试听文字需要 1 到 200 个字', 400)
        try:
            merged = await self._blocking(self._merged)
            # Validate before reserving network work. Preview never persists credentials/config.
            tts_settings.prepare(body.get('provider'), body.get('values', {}), merged)
            if self._tts_preview_slots.locked():
                return self._error('已有试听正在生成，请稍后重试', 429)
            async with self._tts_preview_slots:
                audio = await tts_settings.preview(body.get('provider'), body.get('values', {}), merged, text.strip())
        except (ValueError, TypeError) as e:
            return self._error(str(e), 400)
        except Exception as e:
            from core.providers.tts.cloud_http import SynthesisError
            # Do not include upstream bodies, URLs with keys, or raw exception text.
            message = str(e) if isinstance(e, SynthesisError) else '试听失败，请检查语音服务的网络、密钥和音色权限'
            self.logger.bind(tag=TAG).warning(f'TTS 试听失败: {type(e).__name__}')
            return self._error(message, 502)
        return web.Response(body=audio, content_type='audio/mpeg', headers=SECURITY_HEADERS)

    # ---------- 意图识别 ----------

    async def handle_intent(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        return self._json({"ok": True, **pages.describe_intent(merged), "running": self._running("Intent")})

    async def handle_intent_save(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        mode = str(body.get("mode") or "")
        if mode not in catalog.INTENT_MODES:
            return self._error("意图识别只能选“不用插件”或“用插件”", 400)
        merged = await self._blocking(self._merged)
        notes = []
        try:
            if "functions" in body:
                changed = await self._blocking(pages.save_intent_plugins, body["functions"], merged, self.config_path)
                if changed and mode == "function_call":
                    merged = await self._blocking(self._merged)
                    name = pages.intent_entry_name(merged, "function_call")
                    if (merged.get("selected_module") or {}).get("Intent") == name:
                        notes.append((await self._apply("Intent"))["note"])
            result = await self._switch("Intent", pages.intent_entry_name(merged, mode))
        except KeyError:
            return self._error(f"配置里没有 {mode}", 404)
        except ValueError as e:
            return self._error(str(e), 400)
        if not result["ok"]:
            return self._error(result["note"], 400)
        if result["note"] == "已经在使用" and notes:
            result["note"] = notes[-1]
        return self._json(result)

    # ---------- 插件设置 ----------

    async def handle_plugin_settings(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        try:
            return self._json({"ok": True, **plugin_settings.describe(merged, request.match_info["id"])})
        except KeyError:
            return self._error("没有这个插件设置", 404)

    async def handle_plugin_settings_save(self, request: web.Request):
        """保存后马上同步进运行中的配置和每个在线连接：插件每次调用都读 conn.config，不用重连。"""
        self._require_login(request)
        settings_id = request.match_info["id"]
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            changed = await self._blocking(plugin_settings.save, settings_id, body, merged, self.config_path)
            key = plugin_settings.config_key(settings_id)
        except KeyError:
            return self._error("没有这个插件设置", 404)
        except ValueError as e:
            return self._error(str(e), 400)
        if not changed:
            return self._json({"ok": True, "changed": [], "note": "没有修改",
                               "configured": plugin_settings.configured(merged, settings_id)})
        merged = await self._blocking(self._merged)
        fresh = (merged.get("plugins") or {}).get(key) or {}
        self.config.setdefault("plugins", {})[key] = copy.deepcopy(fresh)
        for conn in connections.all_connections():
            if isinstance(getattr(conn, "config", None), dict):
                conn.config.setdefault("plugins", {})[key] = copy.deepcopy(fresh)
        self.logger.bind(tag=TAG).info(f"管理后台更新了插件设置 {settings_id}: {', '.join(changed)}")
        return self._json({"ok": True, "changed": changed, "note": "已保存，马上生效",
                           "configured": plugin_settings.configured(merged, settings_id)})

    async def handle_plugin_settings_test(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        try:
            merged = await self._blocking(self._merged)
            result = await plugin_settings.run_test(request.match_info["id"], body, merged)
        except KeyError:
            return self._error("这个插件没有测试", 404)
        except ValueError as e:
            return self._error(str(e), 400)
        return self._json({"ok": True, **result})

    # ---------- 在线设备与表情测试 ----------

    def _emotion_progress(self, session_id: str):
        run = self._emotion_runs.get(session_id)
        if not run:
            return None
        task = run["task"]
        return {"running": not task.done(), "stopped": task.cancelled(), "index": run["index"], "total": run["total"],
                "emotion": run["emotion"], "emoji": EMOTION_EMOJI.get(run["emotion"], "")}

    # ---------- 记忆（聊天记录总结） ----------

    async def handle_memory(self, request: web.Request):
        """每台设备的记忆全文、偏好、归属和模型记录。"""
        self._require_login(request)
        merged = await self._blocking(self._merged)
        cfg = chat_history.settings(merged)
        online = {d["device_id"] for d in connections.snapshot() if d.get("device_id")}
        devices = await self._blocking(chat_history.describe_memories)
        mappings = await self._blocking(chat_history.owner_mappings)
        by_device = {str(row.get("device")): row for row in mappings if row.get("device")}
        known = {str(d.get("device")) for d in devices if d.get("device")}
        for device in sorted(online | (set(by_device) - known)):
            if device not in known:
                devices.append({"device": device, "memory": {"summary": "", "pinned": "", "version": 0},
                                "temporary": 0, "expires_at": None, "models": []})
        for d in devices:
            device = str(d["device"])
            mapping = by_device.get(device) or {}
            d["owner"] = mapping.get("owner") or d.get("owner") or device
            d["owner_version"] = mapping.get("version", d.get("owner_version", 0))
            d["preferences"] = await self._blocking(chat_history.list_preferences, d["owner"])
            d["online"] = device in online
            d['label'] = '我的音箱' if len(devices) == 1 else d['device']
        return self._json({"ok": True, "devices": devices,
                           "settings": {k: cfg[k] for k in ("enabled", "summary", "max_messages", "max_tokens",
                                                            "summary_min", "summary_chars", "use_memory")}})

    async def handle_memory_save(self, request: web.Request):
        """{"device", "summary", "version"?}：保存手改的记忆（summary 为空就是清空），下一次请求就用新的。
        带 version 时，打开编辑后自动总结更新过就不保存，返回 409 和现在的记忆。"""
        self._require_login(request)
        body = await self._read_json(request)
        device = str(body.get("device") or "")
        summary = body.get("summary")
        if not device or not isinstance(summary, str):
            return self._error("缺少 device 或 summary", 400)
        if len(summary) > 5000:
            return self._error("记忆最多 5000 字", 400)
        kind = body.get('kind', 'pinned')
        if kind not in ('pinned', 'summary'):
            return self._error('记忆类型不正确', 400)
        version = body.get("version")
        ok, memory = await self._blocking(chat_history.set_memory, device, summary,
                                          int(version) if isinstance(version, int) else None, kind)
        if not ok:
            return self._json({"ok": False, "error": "你编辑期间记忆被自动总结更新了，请看过新内容再改", "memory": memory}, 409)
        return self._json({"ok": True, "memory": memory})

    async def handle_memory_owner(self, request: web.Request):
        """保存设备归属映射；这是记忆分组配置，不是设备认证。"""
        self._require_login(request)
        body = await self._read_json(request)
        device, owner = str(body.get("device") or "").strip(), str(body.get("owner") or "").strip()
        if not device or not owner or len(device) > 300 or len(owner) > 300:
            return self._error("设备和归属不能为空", 400)
        expected = body.get("version")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            return self._error("归属版本不正确", 400)
        old_owner = await self._blocking(chat_history.owner_for_device, device)
        online = [c for c in (connections.get(i["session_id"]) for i in connections.snapshot()
                              if i.get("device_id") == device) if c is not None]
        if old_owner != owner:
            from core.handle.abortHandle import handleAbortMessage
            for conn in online:
                if conn.client_is_speaking or not conn.client_abort:
                    try:
                        await handleAbortMessage(conn, drop_audio=True)
                    except Exception as error:
                        conn.logger.warning(f"设备归属更新前停止旧会话失败: {type(error).__name__}")
        try:
            ok, current = await self._blocking(chat_history.set_device_owner, device, owner, expected)
        except ValueError as e:
            return self._error(str(e), 400)
        if not ok:
            return self._json({"ok": False, "error": "归属刚刚被修改，请刷新后再保存", "mapping": current}, 409)
        if old_owner != owner:
            await self._blocking(task_service.cancel_device_tasks, old_owner, device)
            await self._blocking(chat_history.clear_dialogues, online)
        return self._json({"ok": True, "mapping": current})

    async def handle_memory_preference_save(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        device = str(body.get("device") or "").strip()
        topic, content = str(body.get("topic") or "").strip(), str(body.get("content") or "").strip()
        if not device or not topic or not content:
            return self._error("设备、主题和内容不能为空", 400)
        if len(topic) > 100 or len(content) > 2000:
            return self._error("偏好内容过长", 400)
        owner = await self._blocking(chat_history.owner_for_device, device)
        preference_id = body.get("id")
        if preference_id is not None and (isinstance(preference_id, bool) or not isinstance(preference_id, int) or preference_id < 1):
            return self._error("偏好编号不正确", 400)
        expected = body.get("version")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
            return self._error("偏好版本不正确", 400)
        try:
            ok, row = await self._blocking(chat_history.upsert_preference, owner, topic, content,
                                           preference_id, expected)
        except ValueError as e:
            return self._error(str(e), 400)
        if not ok:
            return self._json({"ok": False, "error": "偏好刚刚被修改，请刷新后再保存", "preference": row}, 409)
        return self._json({"ok": True, "preference": row})

    async def handle_memory_preference_delete(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        device = str(body.get("device") or "").strip()
        preference_id, expected = body.get("id"), body.get("version")
        if not device or isinstance(preference_id, bool) or not isinstance(preference_id, int) or preference_id < 1:
            return self._error("设备和偏好编号不正确", 400)
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
            return self._error("偏好版本不正确", 400)
        owner = await self._blocking(chat_history.owner_for_device, device)
        try:
            ok, row = await self._blocking(chat_history.delete_preference, owner, preference_id, expected)
        except ValueError as e:
            return self._error(str(e), 400)
        if not ok:
            return self._json({"ok": False, "error": "偏好刚刚被修改，请刷新后再保存", "preference": row}, 409)
        return self._json({"ok": True, "preference": row})

    def _task_devices(self):
        online = {str(d.get("device_id")) for d in connections.snapshot() if d.get("device_id")}
        mapped = {str(d.get("device")) for d in chat_history.owner_mappings() if d.get("device")}
        recorded = {str(d.get("device")) for d in chat_history.describe_memories() if d.get("device")}
        tasks = set(task_service.admin_device_ids())
        return sorted(online | mapped | recorded | tasks)

    def _trusted_device_owner(self, device):
        resolver = getattr(self.ws_server, "owner_for_device", None) if self.ws_server is not None else None
        return resolver(device) if callable(resolver) else chat_history.owner_for_device(device)

    async def handle_tasks(self, request: web.Request):
        """列出当前在线或已配置归属设备的任务；任务按所有者隔离。"""
        self._require_login(request)
        requested = str(request.query.get("device") or "").strip()
        devices = [requested] if requested else await self._blocking(self._task_devices)
        if requested and requested not in await self._blocking(self._task_devices):
            return self._error("设备不存在", 404)
        tasks, owners = [], set()
        for device in devices:
            owner = await self._blocking(self._trusted_device_owner, device)
            if owner in owners:
                continue
            owners.add(owner)
            tasks.extend(await self._blocking(task_service.task_status, owner))
        tasks.sort(key=lambda t: t.get("updated", 0), reverse=True)
        worker = getattr(self.ws_server, "task_worker", None) if self.ws_server is not None else None
        return self._json({"ok": True, "devices": devices, "tasks": tasks[:200],
                           "worker_available": bool(worker and worker.is_registered())})

    async def handle_task_start(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        device = str(body.get("device") or "").strip()
        text = str(body.get("request") or "").strip()
        if not device or not text:
            return self._error("设备和任务内容不能为空", 400)
        if device not in await self._blocking(self._task_devices):
            return self._error("设备不存在", 404)
        owner = await self._blocking(self._trusted_device_owner, device)
        try:
            task = await self._blocking(
                task_service.task_start, owner, device, text,
                role="research", deadline=time.time() + ADMIN_TASK_LIFETIME_SECONDS,
                idempotency_key=body.get("idempotency_key"), budget_tokens=ADMIN_TASK_BUDGET_TOKENS)
        except task_service.TaskError as e:
            return self._error(str(e), 400)
        except (TypeError, ValueError) as e:
            return self._error(str(e), 400)
        return self._json({"ok": True, "task": task}, 201)

    async def handle_task_cancel(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        device, task_id = str(body.get("device") or "").strip(), str(body.get("task_id") or "").strip()
        if not device or not task_id:
            return self._error("设备和任务编号不能为空", 400)
        owner = await self._blocking(self._trusted_device_owner, device)
        try:
            task = await self._blocking(task_service.task_cancel, owner, task_id)
        except task_service.TaskError as e:
            return self._error(str(e), 400)
        if task is None:
            return self._error("任务不存在", 404)
        return self._json({"ok": True, "task": task})

    async def handle_task_answer(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        device = str(body.get("device") or "").strip()
        task_id, answer = str(body.get("task_id") or "").strip(), str(body.get("answer") or "").strip()
        if not device or not task_id or not answer:
            return self._error("设备、任务编号和补充信息不能为空", 400)
        owner = await self._blocking(self._trusted_device_owner, device)
        try:
            task = await self._blocking(task_service.task_answer, owner, task_id, answer)
        except task_service.TaskError as e:
            return self._error(str(e), 400)
        if task is None:
            return self._error("任务不存在", 404)
        return self._json({"ok": True, "task": task})

    async def handle_devices(self, request: web.Request):
        self._require_login(request)
        devices = connections.snapshot()
        for d in devices:
            d["emotion_test"] = self._emotion_progress(d["session_id"])
            # 这台设备此刻在跟谁说话（呼名切到的模型）；和大模型页的全局默认是两回事
            d["model_label"] = llm_router.model_label(d["model"]) if d.get("model") else ""
        return self._json({"ok": True, "devices": devices, "now": int(time.time()),
                           "emotions": [{"emotion": name, "emoji": emoji, "label": catalog.EMOTION_LABELS.get(name, name)}
                                        for emoji, name in EMOJI_MAP.items()]})

    def _pairing_store(self):
        return getattr(self.ws_server, "pairing", None) if self.ws_server is not None else None

    async def handle_pairing(self, request: web.Request):
        self._require_login(request)
        merged = await self._blocking(self._merged)
        auth_config = ((merged.get("server") or {}).get("auth") or {})
        store = self._pairing_store()
        records = await self._blocking(store.list) if store is not None else []
        return self._json({
            "ok": True,
            "available": store is not None,
            "auth_enabled": bool(auth_config.get("enabled")),
            "enabled": bool((auth_config.get("device_auth") or {}).get("enabled")),
            "records": records,
        })

    async def handle_pairing_issue(self, request: web.Request):
        self._require_login(request)
        store = self._pairing_store()
        if store is None:
            return self._error("设备配对存储尚未就绪", 503)
        body = await self._read_json(request)
        device = str(body.get("device") or "").strip()
        owner = str(body.get("owner") or "").strip()
        if not device or not owner:
            return self._error("设备和所有者不能为空", 400)
        previous_owner = await self._blocking(store.owner_for_device, device)
        try:
            token, record = await self._blocking(store.issue, device, owner)
        except PairingError as e:
            return self._error(str(e), 400)
        if previous_owner and previous_owner != owner:
            await self._blocking(task_service.cancel_device_tasks, previous_owner, device)
        online = [connections.get(item["session_id"]) for item in connections.snapshot()
                  if item.get("device_id") == device]
        for conn in online:
            if conn is not None:
                try:
                    await conn.close()
                except Exception as exc:
                    conn.logger.warning(f"更新配对令牌后关闭在线连接失败: {type(exc).__name__}")
        return self._json({"ok": True, "token": token, "record": record})

    async def handle_pairing_revoke(self, request: web.Request):
        self._require_login(request)
        store = self._pairing_store()
        if store is None:
            return self._error("设备配对存储尚未就绪", 503)
        body = await self._read_json(request)
        device = str(body.get("device") or "").strip()
        if not device:
            return self._error("设备不能为空", 400)
        old_owner = await self._blocking(store.owner_for_device, device)
        try:
            changed = await self._blocking(store.revoke, device)
        except PairingError as e:
            return self._error(str(e), 400)
        if not changed:
            return self._error("没有找到这台设备的配对记录", 404)
        if old_owner:
            await self._blocking(task_service.cancel_device_tasks, old_owner, device)
        online = [connections.get(item["session_id"]) for item in connections.snapshot()
                  if item.get("device_id") == device]
        for conn in online:
            if conn is not None:
                try:
                    await conn.close()
                except Exception as exc:
                    conn.logger.warning(f"撤销配对后关闭在线连接失败: {type(exc).__name__}")
        return self._json({"ok": True})

    async def handle_pairing_enabled(self, request: web.Request):
        self._require_login(request)
        body = await self._read_json(request)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return self._error("enabled 必须是布尔值", 400)
        was_enabled = bool(getattr(self.ws_server, "device_auth_enable", False))
        if enabled and not was_enabled and self.ws_server is not None:
            store = self._pairing_store()
            paired = {row["device_id"] for row in await self._blocking(store.list)} if store is not None else set()
            online = {item.get("device_id") for item in connections.snapshot() if item.get("device_id")}
            missing = online - paired
            if missing:
                return self._error("先为所有在线设备生成配对令牌，再启用配对校验", 409)
        merged = await self._blocking(self._merged)
        auth_config = ((merged.get("server") or {}).get("auth") or {})
        def save_enabled():
            with config_store.edit_custom(self.config_path) as custom:
                server = custom.setdefault("server", {})
                auth_config = server.setdefault("auth", {})
                device_auth = auth_config.setdefault("device_auth", {})
                device_auth["enabled"] = enabled

        try:
            await self._blocking(save_enabled)
        except (OSError, TypeError, ValueError) as e:
            return self._error(f"保存配对设置失败：{e}", 500)
        if self.ws_server is not None and hasattr(self.ws_server, "update_config"):
            if not await self.ws_server.update_config():
                return self._error("配对设置已保存，但服务热更新失败，请重启服务", 500)
        if enabled and not was_enabled:
            for conn in connections.all_connections():
                try:
                    await conn.close()
                except Exception as exc:
                    conn.logger.warning(f"启用配对校验后关闭旧连接失败: {type(exc).__name__}")
        return self._json({"ok": True, "enabled": enabled})

    @staticmethod
    async def _send_emotion(conn, emotion: str):
        await conn.websocket.send(json.dumps(
            {"type": "llm", "text": EMOTION_EMOJI[emotion], "emotion": emotion, "session_id": conn.session_id}
        ))

    def _online(self, session_id: str):
        conn = connections.get(session_id)
        return conn if conn is not None and getattr(conn, "websocket", None) is not None else None

    def _stop_emotion_test(self, session_id: str) -> bool:
        run = self._emotion_runs.get(session_id)
        if run and not run["task"].done():
            run["task"].cancel()
            return True
        return False

    async def handle_emotion_one(self, request: web.Request):
        """给设备单独发一个表情。"""
        self._require_login(request)
        session_id = request.match_info["session_id"]
        conn = self._online(session_id)
        if conn is None:
            return self._error("设备不在线", 404)
        body = await self._read_json(request)
        emotion = str(body.get("emotion") or "")
        if emotion not in EMOTION_EMOJI:
            return self._error("不支持这个表情", 400)
        self._stop_emotion_test(session_id)
        try:
            await self._send_emotion(conn, emotion)
        except Exception as e:
            return self._error(f"发送失败：{e}", 502)
        return self._json({"ok": True})

    async def handle_emotion_test(self, request: web.Request):
        """依次给设备发送全部情绪消息，用来查看固件实际支持哪些表情。"""
        self._require_login(request)
        session_id = request.match_info["session_id"]
        conn = self._online(session_id)
        if conn is None:
            return self._error("设备不在线", 404)
        body = await self._read_json(request)
        try:
            interval = min(max(float(body.get("interval") or 2), 0.5), 10)
        except (TypeError, ValueError):
            return self._error("interval 需要是数字（秒）", 400)
        self._stop_emotion_test(session_id)
        sequence = [{"emoji": e, "emotion": name} for e, name in EMOJI_MAP.items()]
        run = {"index": 0, "total": len(sequence), "emotion": ""}

        async def play():
            try:
                for i, item in enumerate(sequence, 1):
                    run["index"], run["emotion"] = i, item["emotion"]
                    await self._send_emotion(conn, item["emotion"])
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 测试中途设备断开等
                self.logger.bind(tag=TAG).info(f"表情测试已中止：{e}")

        run["task"] = asyncio.get_running_loop().create_task(play())
        self._emotion_runs = {sid: r for sid, r in self._emotion_runs.items() if connections.get(sid) is not None}
        self._emotion_runs[session_id] = run
        self.logger.bind(tag=TAG).info(f"开始表情测试：设备 {conn.device_id}，共 {len(sequence)} 种，间隔 {interval} 秒")
        return self._json({"ok": True})

    async def handle_emotion_stop(self, request: web.Request):
        self._require_login(request)
        self._stop_emotion_test(request.match_info["session_id"])
        return self._json({"ok": True})
