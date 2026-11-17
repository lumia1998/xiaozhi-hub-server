"""Persistent device pairing credentials.

Pairing tokens are returned only when issued. The database stores a keyed
digest, never the bearer token itself. This store is deliberately separate
from the legacy connection AuthManager and from memory owner mappings.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path


class PairingError(ValueError):
    pass


class PairingStore:
    def __init__(self, path: str | Path, secret: str | bytes):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if not self._secret:
            raise PairingError("配对存储需要服务器密钥")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS device_pairings (
                device_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                token_hash BLOB NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                last_seen REAL
            );
            CREATE INDEX IF NOT EXISTS device_pairings_owner ON device_pairings(owner_id);
            """
        )
        self._private()

    def _private(self):
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                pass

    @staticmethod
    def _text(value, field, maximum=300):
        value = str(value or "").strip()
        if not value or len(value) > maximum or "\x00" in value:
            raise PairingError(f"{field}不能为空或过长")
        return value

    def _digest(self, device_id: str, token: str) -> bytes:
        return hmac.new(self._secret, f"{device_id}\x00{token}".encode("utf-8"), hashlib.sha256).digest()

    def issue(self, device_id: str, owner_id: str):
        device_id = self._text(device_id, "设备")
        owner_id = self._text(owner_id, "所有者")
        token = secrets.token_urlsafe(32)
        now = time.time()
        digest = self._digest(device_id, token)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO device_pairings(device_id,owner_id,token_hash,enabled,created,updated,last_seen) "
                    "VALUES(?,?,?,1,?,?,NULL) ON CONFLICT(device_id) DO UPDATE SET owner_id=excluded.owner_id, "
                    "token_hash=excluded.token_hash,enabled=1,updated=excluded.updated,last_seen=NULL",
                    (device_id, owner_id, digest, now, now),
                )
                row = self._db.execute(
                    "SELECT device_id,owner_id,enabled,created,updated,last_seen FROM device_pairings WHERE device_id=?",
                    (device_id,),
                ).fetchone()
                self._db.execute("COMMIT")
                self._private()
                return token, dict(row)
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def verify(self, device_id: str, token: str) -> bool:
        try:
            device_id = self._text(device_id, "设备")
            token = self._text(token, "配对令牌", 1000)
        except PairingError:
            return False
        with self._lock:
            row = self._db.execute(
                "SELECT token_hash FROM device_pairings WHERE device_id=? AND enabled=1", (device_id,)
            ).fetchone()
            valid = bool(row and hmac.compare_digest(bytes(row[0]), self._digest(device_id, token)))
            if valid:
                now = time.time()
                self._db.execute("UPDATE device_pairings SET last_seen=?,updated=? WHERE device_id=?", (now, now, device_id))
                self._private()
            return valid

    def owner_for_device(self, device_id: str):
        """Return the authenticated account owner for a currently paired device."""
        device_id = self._text(device_id, "设备")
        with self._lock:
            row = self._db.execute(
                "SELECT owner_id FROM device_pairings WHERE device_id=? AND enabled=1", (device_id,)
            ).fetchone()
            return row[0] if row else None

    def set_owner(self, device_id: str, owner_id: str) -> bool:
        """Change the trusted owner binding without rotating the device token."""
        device_id = self._text(device_id, "设备")
        owner_id = self._text(owner_id, "所有者")
        with self._lock:
            cur = self._db.execute(
                "UPDATE device_pairings SET owner_id=?,updated=? WHERE device_id=? AND enabled=1",
                (owner_id, time.time(), device_id),
            )
            self._private()
            return cur.rowcount == 1

    def revoke(self, device_id: str) -> bool:
        device_id = self._text(device_id, "设备")
        with self._lock:
            cur = self._db.execute("UPDATE device_pairings SET enabled=0,updated=? WHERE device_id=?", (time.time(), device_id))
            self._private()
            return cur.rowcount == 1

    def list(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT device_id,owner_id,enabled,created,updated,last_seen FROM device_pairings ORDER BY device_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def close(self):
        with self._lock:
            self._db.close()


__all__ = ["PairingStore", "PairingError"]
