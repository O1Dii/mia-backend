"""Device-style linking: short-lived, revocable tokens bound to one user's installation."""

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from uuid import uuid4

from app.web_poc.runtime import Workspace


@dataclass
class Grant:
    workspace: Workspace
    plugin_id: str
    expires: float
    id: str = field(default_factory=lambda: str(uuid4()))
    revoked: bool = False


@dataclass
class Pairing:
    plugin_id: str
    user_code: str
    expires: float
    token: str | None = field(default=None, repr=False)
    subject_id: str | None = None
    approved: bool = False


class ExtensionAuth:
    def __init__(self) -> None:
        self.pending: dict[str, Pairing] = {}
        self.grants: dict[str, Grant] = {}

    def prune(self) -> None:
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if v.expires > now}
        self.grants = {
            k: v
            for k, v in self.grants.items()
            if v.expires > now and not v.revoked and v.workspace.expires > now
        }

    def start(self, plugin_id: str) -> dict[str, str | int]:
        self.prune()
        if len(self.pending) >= 64:
            raise ValueError("Слишком много запросов привязки.")
        device_code = secrets.token_urlsafe(32)
        user_code = secrets.token_hex(4).upper()
        self.pending[hashlib.sha256(device_code.encode()).hexdigest()] = Pairing(
            plugin_id, user_code, time.monotonic() + 300
        )
        return {
            "device_code": device_code,
            "user_code": user_code,
            "expires_in": 300,
            "interval": 2,
        }

    def inspect(self, user_code: str) -> Pairing:
        self.prune()
        result = next((p for p in self.pending.values() if p.user_code == user_code.upper()), None)
        if result is None or result.approved:
            raise ValueError("Код не найден, истёк или уже использован.")
        return result

    def approve(self, workspace: Workspace, user_code: str) -> Grant:
        pairing = self.inspect(user_code)
        if pairing.plugin_id not in workspace.installed:
            raise ValueError("Сначала установите этот плагин в своём пространстве.")
        if len([g for g in self.grants.values() if g.workspace is workspace]) >= 20:
            raise ValueError("Лимит: 20 подключённых приложений.")
        token = secrets.token_urlsafe(40)
        grant = Grant(workspace, pairing.plugin_id, min(workspace.expires, time.monotonic() + 3600))
        self.grants[hashlib.sha256(token.encode()).hexdigest()] = grant
        pairing.token, pairing.subject_id, pairing.approved = token, workspace.subject_id, True
        return grant

    def poll(self, device_code: str) -> dict[str, str]:
        self.prune()
        hashed = hashlib.sha256(device_code.encode()).hexdigest()
        pairing = self.pending.get(hashed)
        if pairing is None:
            raise ValueError("Привязка истекла. Начните заново.")
        if pairing.token is None:
            return {"status": "pending"}
        del self.pending[hashed]
        return {
            "status": "approved",
            "access_token": pairing.token,
            "subject_id": pairing.subject_id or "",
            "plugin_id": pairing.plugin_id,
        }

    def authenticate(self, token: str) -> Grant:
        self.prune()
        grant = self.grants.get(hashlib.sha256(token.encode()).hexdigest())
        if grant is None or grant.plugin_id not in grant.workspace.installed:
            raise ValueError("Приложение не подключено, отозвано или сессия истекла.")
        return grant

    def revoke_installation(self, workspace: Workspace, plugin_id: str) -> None:
        for grant in self.grants.values():
            if grant.workspace is workspace and grant.plugin_id == plugin_id:
                grant.revoked = True
