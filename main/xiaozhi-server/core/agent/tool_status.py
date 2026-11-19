import asyncio
import json
import uuid
from contextlib import asynccontextmanager


@asynccontextmanager
async def observing(conn, name):
    categories = {'weather': 'weather', 'get_weather': 'weather', 'web_search': 'search',
                  'home_assistant': 'device', 'hass_get_state': 'device', 'hass_set_state': 'device'}
    category = categories.get(name, 'extension' if name.startswith('mcp_') else None)
    if not category or (getattr(conn, 'features', {}) or {}).get('tool_status') != 1:
        yield
        return
    payload = {'type': 'tool_status', 'id': uuid.uuid4().hex, 'category': category, 'session_id': conn.session_id}
    timing = getattr(conn, 'turn_timing', None)
    if timing:
        payload['timing_id'] = timing.id
    sending = False

    async def notify(state):
        try:
            await asyncio.wait_for(conn.websocket.send(json.dumps({**payload, 'state': state})), timeout=1)
            return True
        except Exception:
            return False

    async def show():
        nonlocal sending
        await asyncio.sleep(.5)
        if timing and (getattr(conn, 'turn_timing', None) is not timing or conn.turn_gen != timing.generation):
            return False
        sending = True
        return await notify('start')

    pending = asyncio.create_task(show())
    try:
        yield
    finally:
        if not sending:
            pending.cancel()
        shown, = await asyncio.gather(pending, return_exceptions=True)
        if shown is True:
            await notify('end')
