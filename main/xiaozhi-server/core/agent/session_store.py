"""记录"设备 → 上次用的模型"，保存到 JSON 文件，服务重启后仍有效。"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

# 同一个文件的所有实例共用一把锁：都是"读整个文件 → 改自己那一块 → 整个写回"，锁不共享时并发写会互相覆盖
_locks = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = os.path.abspath(path)
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


class SessionStore:
    def __init__(self, path: Path, namespace: str):
        self.path = Path(path)
        self.namespace = namespace
        self._lock = _lock_for(self.path)

    def _read_all(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_all(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".agent_sessions.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, device_key: str) -> Optional[dict]:
        with self._lock:
            return self._read_all().get(self.namespace, {}).get(device_key)

    def put(self, device_key: str, record: dict, now: float = None) -> None:
        """保存任意记录（如设备上次用的模型），自动加上 last_active。"""
        with self._lock:
            data = self._read_all()
            data.setdefault(self.namespace, {})[device_key] = {
                **record, "last_active": now if now is not None else time.time()}
            self._write_all(data)

    def forget(self, device_key: str) -> None:
        with self._lock:
            data = self._read_all()
            if data.get(self.namespace, {}).pop(device_key, None) is not None:
                self._write_all(data)
