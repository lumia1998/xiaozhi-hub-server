"""Device command hook: match -> HA REST -> {handled, reply}; never invokes a model."""
from core.agent import ha_commands

NAME = 'home_assistant'


def match(conn, text):
    config = (conn.config.get('plugins') or {}).get('home_assistant') or {}
    if (config.get('direct_commands') or {}).get('enabled') is not True:
        return None
    return ha_commands.parse(text, config)


async def intercept(conn, text, stale=lambda: False):
    command = match(conn, text)
    if command is None:
        return {'handled': False}
    config = conn.config['plugins']['home_assistant']
    try:
        outcome = await ha_commands.execute(command, config, stale)
    except ha_commands.Superseded:
        return {'handled': True, 'reply': None, 'cancelled': True}
    reply = str(outcome)
    # A failed/uncertain HTTP request is still handled: never fall back and repeat it via LLM.
    return {'handled': True, 'reply': reply, 'action': command.action, 'entity': command.device.entity_id,
            'confirmed': bool(getattr(outcome, 'confirmed', False))}
