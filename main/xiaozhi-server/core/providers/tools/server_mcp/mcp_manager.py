"""Per-connection MCP tools with stable namespaces and no replay of side effects."""
import asyncio
import copy
import hashlib
import threading
import time

from config.logger import setup_logging
from core.agent.extensions import mcp_configs, revision
from .mcp_client import ServerMCPClient

logger = setup_logging()
_catalogs = {}
_catalog_lock = threading.Lock()
CATALOG_TTL = 600


class ServerMCPManager:
    def __init__(self, conn):
        self.conn = conn
        self.clients = {}
        self.tools = []
        self.routes = {}
        self.configs = {}
        self.locks = {}
        self.catalog = {}
        self.closed = False

    def load_config(self):
        return mcp_configs('api')

    async def _connect(self, name, config):
        if self.closed:
            raise RuntimeError('连接已结束')
        client = ServerMCPClient(config)
        try:
            await asyncio.wait_for(client.initialize(), timeout=config.get('timeout', 15))
            if self.closed:
                raise RuntimeError('连接已结束')
            rows = {}
            for definition in client.get_available_tools():
                local = definition['function']['name']
                original = client.name_mapping[local]
                if 'allowed_tools' in config and original not in config['allowed_tools']:
                    continue
                rows[local] = (original, definition)
            self.clients[name] = client
            self.catalog[name] = rows
            with _catalog_lock:
                if len(_catalogs) >= 128:
                    del _catalogs[next(iter(_catalogs))]
                _catalogs[(name, revision(config))] = (time.monotonic() + CATALOG_TTL, rows)
            return rows
        except BaseException:
            await client.cleanup()
            raise

    async def _init_server(self, name, config):
        if config.get('allowed_tools') == []:
            return
        self.locks[name] = asyncio.Lock()
        with _catalog_lock:
            expires, rows = _catalogs.get((name, revision(config)), (0, {}))
        try:
            if expires <= time.monotonic():
                rows = await self._connect(name, config)
            for local, (original, tool) in rows.items():
                digest = hashlib.sha256((name + ':' + local).encode()).hexdigest()[:12]
                exposed = 'mcp_' + name[:18] + '_' + local[:24] + '_' + digest
                definition = copy.deepcopy(tool)
                definition['function']['name'] = exposed
                self.routes[exposed] = (name, local, original, tool)
                self.tools.append(definition)
        except Exception as exc:
            logger.warning(f'MCP {name} unavailable ({type(exc).__name__})')

    async def initialize_servers(self):
        self.configs = self.load_config()
        await asyncio.gather(*(self._init_server(name, config) for name, config in self.configs.items()))
        handler = getattr(self.conn, 'func_handler', None)
        if handler:
            handler.tool_manager.refresh_tools()

    def get_all_tools(self):
        return self.tools

    def is_mcp_tool(self, name):
        return name in self.routes

    async def execute_tool(self, name, arguments):
        if name not in self.routes:
            raise ValueError('MCP 工具不存在')
        server, local, original, tool = self.routes[name]
        started = time.monotonic()
        timing = getattr(self.conn, 'turn_timing', None)
        async with self.locks[server]:
            cold = server not in self.clients
            if server not in self.clients:
                try:
                    await self._connect(server, self.configs[server])
                except Exception as exc:
                    logger.warning(f'MCP {server} unavailable ({type(exc).__name__})')
                    raise RuntimeError('MCP 服务暂时无法连接，工具未执行') from None
            if self.catalog[server].get(local) != (original, tool):
                raise ValueError('MCP 工具定义已变更，请重新连接后再试')
            client = self.clients[server]
        if timing:
            timing.mark('mcp_connect_cold' if cold else 'mcp_connected', started)
        # A timeout may follow a successful remote operation. Never replay automatically.
        try:
            return await asyncio.wait_for(client.call_tool(local, arguments), timeout=client.config.get('timeout', 15))
        except Exception as exc:
            logger.warning(f'MCP tool {name} failed ({type(exc).__name__})')
            raise RuntimeError('MCP 调用失败，未自动重试；操作可能已经执行，请先查询状态') from None
        finally:
            if timing:
                timing.mark('mcp_total', started)

    async def cleanup_all(self):
        self.closed = True
        await asyncio.gather(*(c.cleanup() for c in self.clients.values()), return_exceptions=True)
        self.clients.clear()
        self.tools.clear()
        self.routes.clear()
