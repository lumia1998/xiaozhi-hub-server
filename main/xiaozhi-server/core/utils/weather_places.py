"""Small, atomic disk cache for successfully resolved weather locations."""
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from config.config_loader import get_project_dir

PATH = Path(get_project_dir()) / 'data/weather_places.json'
TTL = 30 * 86400
LIMIT = 128
_lock = threading.RLock()


def _read():
    try:
        raw = PATH.read_bytes()
        if len(raw) > 256 * 1024: return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _valid(row):
    try:
        return (isinstance(row, dict) and isinstance(row['name'], str) and bool(row['name'])
                and len(row['name']) <= 300 and math.isfinite(row['lat']) and math.isfinite(row['lon'])
                and -90 <= row['lat'] <= 90 and -180 <= row['lon'] <= 180
                and 0 <= time.time() - row['saved'] < TTL)
    except (KeyError, TypeError, ValueError):
        return False


def get(query):
    with _lock:
        row = _read().get(query)
        return dict(row) if _valid(row) else None


def put(query, place):
    row = {'name': place.name, 'lat': place.lat, 'lon': place.lon, 'saved': time.time()}
    if not _valid(row) or not query or len(query) > 100: return
    with _lock:
        rows = {k: v for k, v in _read().items() if _valid(v)}
        rows[query] = row
        rows = dict(sorted(rows.items(), key=lambda item: item[1]['saved'])[-LIMIT:])
        PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=PATH.parent, prefix='.weather-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(rows, f, ensure_ascii=False)
                f.flush(); os.fsync(f.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, PATH)
        finally:
            if os.path.exists(temp): os.unlink(temp)
