from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_device_error_keeps_camera_diagnostic(monkeypatch):
    monkeypatch.setattr('config.logger.setup_logging', lambda: Mock())
    from core.providers.tools.device_mcp import mcp_handler

    async def register(identifier, future):
        future.set_result({'isError': True, 'content': [
            {'type': 'text', 'text': 'Ubuntu 尚未识别到可用摄像头'}]})

    client = SimpleNamespace(is_ready=AsyncMock(return_value=True), has_tool=lambda _: True,
                             get_next_id=AsyncMock(return_value=3), register_call_result_future=register,
                             name_mapping={}, cleanup_call_result=AsyncMock())
    monkeypatch.setattr(mcp_handler, 'send_mcp_message', AsyncMock())
    with pytest.raises(RuntimeError, match='Ubuntu 尚未识别到可用摄像头'):
        await mcp_handler.call_mcp_tool(SimpleNamespace(), client, 'self.camera.take_photo')
    client.cleanup_call_result.assert_awaited_once_with(3)
