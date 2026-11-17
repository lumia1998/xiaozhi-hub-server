import asyncio
import logging
from pathlib import Path

import websockets
from config.logger import setup_logging


class SuppressInvalidHandshakeFilter(logging.Filter):
    """过滤掉无效握手错误日志（如HTTPS访问WS端口）"""

    def filter(self, record):
        msg = record.getMessage()
        suppress_keywords = [
            "opening handshake failed",
            "did not receive a valid HTTP request",
            "connection closed while reading HTTP request",
            "line without CRLF",
        ]
        return not any(keyword in msg for keyword in suppress_keywords)


def _setup_websockets_logger():
    """配置 websockets 相关的所有 logger，过滤无效握手错误"""
    filter_instance = SuppressInvalidHandshakeFilter()
    for logger_name in ["websockets", "websockets.server", "websockets.client"]:
        logger = logging.getLogger(logger_name)
        logger.addFilter(filter_instance)


_setup_websockets_logger()


from core.connection import ConnectionHandler
from config.config_loader import get_config_from_api_async
from core.auth import AuthManager, AuthenticationError
from core.device_pairing import PairingStore
from config.config_loader import get_project_dir
from core.utils.modules_initialize import initialize_modules
from core.utils.util import check_vad_update, check_asr_update
from core.agent.task_worker import TaskWorker

TAG = __name__


class WebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging(config)
        self.config_lock = asyncio.Lock()
        modules = initialize_modules(
            self.logger,
            self.config,
            "VAD" in self.config["selected_module"],
            "ASR" in self.config["selected_module"],
            "LLM" in self.config["selected_module"],
            False,
            "Memory" in self.config["selected_module"],
            "Intent" in self.config["selected_module"],
        )
        self._vad = modules["vad"] if "vad" in modules else None
        self._asr = modules["asr"] if "asr" in modules else None
        self._llm = modules["llm"] if "llm" in modules else None
        self._intent = modules["intent"] if "intent" in modules else None
        self._memory = modules["memory"] if "memory" in modules else None

        self.pairing_path = Path(get_project_dir()) / "data" / "device_auth.db"
        self._refresh_auth_settings(self.config)
        self.task_worker = TaskWorker(self.config)
        self.task_worker.owner_resolver = self.owner_for_device

    def _refresh_auth_settings(self, config):
        auth_config = (config.get("server") or {}).get("auth") or {}
        self.auth_enable = bool(auth_config.get("enabled", False))
        self.device_auth_enable = bool((auth_config.get("device_auth") or {}).get("enabled", False))
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = str((config.get("server") or {}).get("auth_key") or "")
        expire_seconds = auth_config.get("expire_seconds")
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)
        # Keep the pairing store available to the admin UI while enforcement is
        # disabled, so credentials can be provisioned before the explicit switch.
        if not secret_key:
            if getattr(self, "pairing", None) is not None:
                self.pairing.close()
            self.pairing = None
            self._pairing_secret = ""
        elif getattr(self, "pairing", None) is None or getattr(self, "_pairing_secret", None) != secret_key:
            if getattr(self, "pairing", None) is not None:
                self.pairing.close()
            self.pairing = PairingStore(self.pairing_path, secret_key)
            self._pairing_secret = secret_key
        worker = getattr(self, "task_worker", None)
        if worker is not None:
            worker.owner_resolver = self.owner_for_device

    def owner_for_device(self, device_id):
        """Resolve the authenticated principal when pairing is enforced."""
        if self.device_auth_enable and self.pairing is not None:
            owner = self.pairing.owner_for_device(device_id)
            if owner:
                return owner
        from core.agent import chat_history
        return chat_history.owner_for_device(device_id)

    async def start(self):
        server_config = self.config["server"]
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("port", 8000))

        from core.agent.chat_history import retention_loop
        retention = asyncio.create_task(retention_loop(self.config), name='memory-retention')
        self.task_worker.start()
        try:
            async with websockets.serve(
                self._handle_connection,
                host,
                port,
                process_request=self._http_response,
                ping_interval=None,
            ):
                await asyncio.Future()
        finally:
            self.task_worker.stop()
            if getattr(self, "pairing", None) is not None:
                self.pairing.close()
            retention.cancel()
            await asyncio.gather(retention, return_exceptions=True)

    async def _handle_connection(self, websocket: websockets.ServerConnection):
        headers = dict(websocket.request.headers)
        if headers.get("device-id", None) is None:
            # 尝试从 URL 的查询参数中获取 device-id
            from urllib.parse import parse_qs, urlparse

            # 从 WebSocket 请求中获取路径
            request_path = websocket.request.path
            if not request_path:
                self.logger.bind(tag=TAG).error("无法获取请求路径")
                await websocket.close()
                return
            parsed_url = urlparse(request_path)
            query_params = parse_qs(parsed_url.query)
            if "device-id" not in query_params:
                await websocket.send("端口正常，如需测试连接，请启动digital-human测试")
                await websocket.close()
                return
            else:
                websocket.request.headers["device-id"] = query_params["device-id"][0]
            if "client-id" in query_params:
                websocket.request.headers["client-id"] = query_params["client-id"][0]
            if "authorization" in query_params:
                websocket.request.headers["authorization"] = query_params[
                    "authorization"
                ][0]

        """处理新连接，每次创建独立的ConnectionHandler"""
        # 先认证，后建立连接
        try:
            authenticated_owner = await self._handle_auth(websocket)
        except AuthenticationError:
            await websocket.send("认证失败")
            await websocket.close()
            return
        # 创建ConnectionHandler时传入当前server实例
        handler = ConnectionHandler(
            self.config,
            self._vad,
            self._asr,
            self._llm,
            self._memory,
            self._intent,
            self,  # 传入server实例
        )
        handler.authenticated_owner_id = authenticated_owner
        try:
            await handler.handle_connection(websocket)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"处理连接时出错: {e}")
        finally:
            # 强制关闭连接（如果还没有关闭的话）
            try:
                # 安全地检查WebSocket状态并关闭
                if hasattr(websocket, "closed") and not websocket.closed:
                    await websocket.close()
                elif hasattr(websocket, "state") and websocket.state.name != "CLOSED":
                    await websocket.close()
                else:
                    # 如果没有closed属性，直接尝试关闭
                    await websocket.close()
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"服务器端强制关闭连接时出错: {close_error}"
                )

    async def _http_response(self, websocket, request_headers):
        # 检查是否为 WebSocket 升级请求
        if request_headers.headers.get("connection", "").lower() == "upgrade":
            # 如果是 WebSocket 请求，返回 None 允许握手继续
            return None
        else:
            # 如果是普通 HTTP 请求，返回 "server is running"
            return websocket.respond(200, "Server is running\n")

    async def update_config(self) -> bool:
        """更新服务器配置并重新初始化组件

        Returns:
            bool: 更新是否成功
        """
        try:
            async with self.config_lock:
                # 重新获取配置（使用异步版本）
                new_config = await get_config_from_api_async(self.config)
                if new_config is None:
                    self.logger.bind(tag=TAG).error("获取新配置失败")
                    return False
                self.logger.bind(tag=TAG).info(f"获取新配置成功")
                # 检查 VAD 和 ASR 类型是否需要更新
                update_vad = check_vad_update(self.config, new_config)
                update_asr = check_asr_update(self.config, new_config)
                self.logger.bind(tag=TAG).info(
                    f"检查VAD和ASR类型是否需要更新: {update_vad} {update_asr}"
                )
                # 更新配置
                self.config = new_config
                self._refresh_auth_settings(new_config)
                http_server = getattr(self, "http_server", None)
                if http_server is not None and hasattr(http_server, "update_config"):
                    http_server.update_config(new_config)
                self.task_worker.update_config(new_config)
                # 重新初始化组件
                modules = initialize_modules(
                    self.logger,
                    new_config,
                    update_vad,
                    update_asr,
                    "LLM" in new_config["selected_module"],
                    False,
                    "Memory" in new_config["selected_module"],
                    "Intent" in new_config["selected_module"],
                )

                # 更新组件实例
                if "vad" in modules:
                    self._vad = modules["vad"]
                if "asr" in modules:
                    self._asr = modules["asr"]
                if "llm" in modules:
                    self._llm = modules["llm"]
                if "intent" in modules:
                    self._intent = modules["intent"]
                if "memory" in modules:
                    self._memory = modules["memory"]
                self.logger.bind(tag=TAG).info(f"更新配置任务执行完毕")
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新服务器配置失败: {str(e)}")
            return False

    async def _handle_auth(self, websocket: websockets.ServerConnection):
        # 先认证，后建立连接
        headers = dict(websocket.request.headers)
        device_id = headers.get("device-id", None)
        client_id = headers.get("client-id", None)
        if self.device_auth_enable:
            pairing_token = headers.get("x-device-pairing-token", "")
            if not self.pairing or not self.pairing.verify(device_id or "", pairing_token):
                raise AuthenticationError("Device pairing required")
            owner = self.pairing.owner_for_device(device_id or "")
            if not owner:
                raise AuthenticationError("Device pairing owner unavailable")
            return owner
        if not self.auth_enable:
            return None
        # Pairing is an additional device credential. A legacy allowed-device
        # entry can skip the HMAC token, but never skips pairing verification.
        if not (self.allowed_devices and device_id in self.allowed_devices):
            token = headers.get("authorization", "")
            if token.startswith("Bearer "):
                token = token[7:]
            else:
                raise AuthenticationError("Missing or invalid Authorization header")
            if not self.auth.verify_token(token, client_id=client_id, username=device_id):
                raise AuthenticationError("Invalid token")
        return None
