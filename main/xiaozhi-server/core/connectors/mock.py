"""A deterministic connector for local wiring and contract tests, no real account."""
from __future__ import annotations

from core.credentials import CredentialError, CredentialManager


class MockConnector:
    provider = "mock"

    def __init__(self, credentials: CredentialManager):
        self.credentials = credentials

    def connect(self, owner_id, alias="测试账号", secret="mock-secret", scopes=()):
        ref = self.credentials.connect(owner_id, self.provider, alias, secret, scopes=scopes)
        return ref.public()

    def execute(self, owner_id, ref_id, operation, *, required_scope=None):
        """Return structured fake data; the secret is never returned or logged."""
        if operation not in ("status", "echo"):
            raise CredentialError("mock connector 不支持该操作")
        self.credentials.resolve(owner_id, ref_id, provider=self.provider, scope=required_scope)
        return {"provider": self.provider, "operation": operation, "status": "ok",
                "message": "mock connector 已完成演示操作"}


__all__ = ["MockConnector"]
