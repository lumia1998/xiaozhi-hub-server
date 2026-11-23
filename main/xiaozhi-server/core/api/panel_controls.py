from core.agent import ha_commands


def describe(config, device, row, room):
    state = row.get('state')
    attrs = row.get('attributes') or {}
    on = True if state in ('on', 'playing', 'paused', 'idle') else False if state in ('off', 'standby') else None
    features = attrs.get('supported_features') or 0
    write = on is not None and ha_commands.allowed(config, device.entity_id, write=True)
    power = device.domain != 'media_player' or bool(features & (128 if on else 256))
    result = dict(id=device.entity_id, name=device.name, room=room, on=on, writable=write and power)
    level = None
    if device.domain == 'light' and set(attrs.get('supported_color_modes') or []) - {'onoff', 'unknown'}:
        level = ('brightness', 0 if on is False else attrs.get('brightness'), 255)
    elif device.domain == 'media_player' and features & 4:
        level = ('volume', attrs.get('volume_level'), 1)
    if level:
        kind, value, maximum = level
        if type(value) in (float, int) and 0 <= value <= maximum and on is not None:
            result['level'] = dict(kind=kind, value=round(100 * value / maximum), writable=write)
    return result
