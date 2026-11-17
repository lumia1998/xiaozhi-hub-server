"""Per-connection MCP tools with stable namespaces and no replay of side effects."""
import asyncio
import copy
import hashlib

from config.logger import setup_logging
from core.agent.extensions import mcp_configs
from .mcp_client import ServerMCPClient

logger = setup_logging()


class ServerMCPManager:
    def __init__(self, conn):
        self.conn = conn
        self.clients = {}
        self.tools = []
        self.routes = {}

    def load_config(self):
        return mcp_configs('api')

    async def _init_server(self, name, config):
        client = ServerMCPClient(config)
        try:
            await asyncio.wait_for(client.initialize(), timeout=config.get('timeout', 15))
            self.clients[name] = client
            for tool in client.get_available_tools():
                original = tool['function']['name']
                digest = hashlib.sha256((name + ':' + original).encode()).hexdigest()[:12]
                exposed = 'mcp_' + name[:18] + '_' + original[:24] + '_' + digest
                definition = copy.deepcopy(tool)
                definition['function']['name'] = exposed
                self.routes[exposed] = (name, original)
                self.tools.append(definition)
        except Exception as exc:
            logger.warning(f'MCP {name} unavailable ({type(exc).__name__})')
            await client.cleanup()

    async def initialize_servers(self):
        await asyncio.gather(*(self._init_server(n, c) for n, c in self.load_config().items()))
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
        server, original = self.routes[name]
        client = self.clients[server]
        # A timeout may follow a successful remote operation. Never replay automatically.
        try:
            return await asyncio.wait_for(client.call_tool(original, arguments), timeout=client.config.get('timeout', 15))
        except Exception as exc:
            logger.warning(f'MCP tool {name} failed ({type(exc).__name__})')
            raise RuntimeError('MCP 调用失败，未自动重试；操作可能已经执行，请先查询状态') from None

    async def cleanup_all(self):
        await asyncio.gather(*(c.cleanup() for c in self.clients.values()), return_exceptions=True)
        self.clients.clear()
        self.tools.clear()
        self.routes.clear()
