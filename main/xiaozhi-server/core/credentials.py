"""Owner-bound credential references and storage adapters.

The Hub passes connector code an opaque reference, never a password or token in
task prompts, memory, logs, or TTS. A production deployment should inject the
system keyring adapter; the in-memory adapter exists only for local connectors
and isolated tests.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Protocol


_PROVIDER = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_ALIAS = re.compile(r"^[^\x00\r\n]{1,120}$")


class CredentialError(ValueError):
    """Invalid, missing, expired, or unauthorized credential reference."""


@dataclass(frozen=True)
class CredentialRef:
    ref_id: str
    owner_id: str
    provider: str
    alias: str
    scopes: tuple[str, ...]
    status: str
    expires_at: Optional[float]

    def public(self) -> dict:
        """Metadata safe for admin UI and model capability descriptions."""
        return {"ref_id": self.ref_id, "provider": self.provider, "alias": self.alias,
                "scopes": list(self.scopes), "status": self.status, "expires_at": self.expires_at}


class SecretBackend(Protocol):
    def put(self, ref_id: str, secret: str) -> None: ...
    def get(self, ref_id: str) -> str: ...
    def delete(self, ref_id: str) -> None: ...


class InMemorySecretBackend:
    """Non-persistent backend for mock connectors and isolated development."""
    def __init__(self):
        self._values = {}
        self._lock = threading.RLock()

    def put(self, ref_id, secret):
        with self._lock:
            self._values[str(ref_id)] = str(secret)

    def get(self, ref_id):
        with self._lock:
            try:
                return self._values[str(ref_id)]
            except KeyError:
                raise CredentialError("凭证不存在") from None

    def delete(self, ref_id):
        with self._lock:
            self._values.pop(str(ref_id), None)


class SystemKeyringBackend:
    """Adapter for an OS keyring; importing keyring remains optional."""
    def __init__(self, service="xiaozhi-hub-credentials", keyring_module=None):
        if keyring_module is None:
            try:
                import keyring as keyring_module
            except ImportError:
                raise CredentialError("系统密钥服务不可用，请安装 keyring 或注入其他后端") from None
        self._keyring = keyring_module
        self._service = service

    def put(self, ref_id, secret):
        self._keyring.set_password(self._service, str(ref_id), str(secret))

    def get(self, ref_id):
        value = self._keyring.get_password(self._service, str(ref_id))
        if not value:
            raise CredentialError("凭证不存在")
        return value

    def delete(self, ref_id):
        self._keyring.delete_password(self._service, str(ref_id))


class CredentialManager:
    """Owner-scoped metadata plus an injected secret backend."""
    def __init__(self, backend: SecretBackend):
        self.backend = backend
        self._refs: dict[str, CredentialRef] = {}
        self._lock = threading.RLock()

    def connect(self, owner_id, provider, alias, secret, *, scopes=(), expires_at=None) -> CredentialRef:
        owner_id = self._text(owner_id, "owner_id", 300)
        provider = self._text(provider, "provider", 80)
        alias = self._text(alias, "alias", 120)
        if not _PROVIDER.fullmatch(provider) or not _ALIAS.fullmatch(alias):
            raise CredentialError("账号连接标识格式不正确")
        secret = self._text(secret, "secret", 10000)
        scope_values = tuple(sorted({self._text(s, "scope", 120) for s in scopes}))
        if len(scope_values) > 64:
            raise CredentialError("授权范围过多")
        if expires_at is not None and float(expires_at) <= time.time():
            raise CredentialError("授权已过期")
        ref = CredentialRef(secrets.token_urlsafe(18), owner_id, provider, alias, scope_values,
                            "connected", float(expires_at) if expires_at is not None else None)
        with self._lock:
            self.backend.put(ref.ref_id, secret)
            self._refs[ref.ref_id] = ref
        return ref

    def describe(self, owner_id):
        owner_id = str(owner_id)
        with self._lock:
            return [ref.public() for ref in self._refs.values() if ref.owner_id == owner_id]

    def resolve(self, owner_id, ref_id, *, provider=None, scope=None) -> str:
        """Resolve a secret only for trusted connector code, never for model output."""
        with self._lock:
            ref = self._refs.get(str(ref_id))
            if ref is None or ref.owner_id != str(owner_id):
                raise CredentialError("凭证引用无权访问")
            if provider is not None and ref.provider != provider:
                raise CredentialError("凭证服务不匹配")
            if ref.status != "connected" or (ref.expires_at is not None and ref.expires_at <= time.time()):
                raise CredentialError("账号授权已失效，请重新连接")
            if scope is not None and scope not in ref.scopes:
                raise CredentialError("账号没有所需授权范围")
            return self.backend.get(ref.ref_id)

    def disconnect(self, owner_id, ref_id):
        with self._lock:
            ref = self._refs.get(str(ref_id))
            if ref is None or ref.owner_id != str(owner_id):
                return False
            self.backend.delete(ref.ref_id)
            self._refs[ref.ref_id] = CredentialRef(ref.ref_id, ref.owner_id, ref.provider,
                                                   ref.alias, ref.scopes, "revoked", ref.expires_at)
            return True

    @staticmethod
    def _text(value, label, limit):
        value = str(value or "").strip()
        if not value or len(value) > limit or "\x00" in value:
            raise CredentialError(f"{label}格式不正确")
        return value


__all__ = ["CredentialError", "CredentialRef", "CredentialManager", "InMemorySecretBackend", "SystemKeyringBackend"]
