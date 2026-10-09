"""Plugin account connections: start, callback, refresh, headers for the router, revoke.

Tokens live in ``plugin_connections.secret_ciphertext`` sealed with the user's DEK
(M11 ``KeyVault``). Nothing here returns or logs a token except :meth:`headers`, which
hands the router a bearer for exactly one request. ``installations.status`` follows the
connection: ``needs_connection`` until the callback completes, ``active`` afterwards (when
the consent was already given), back to ``needs_connection`` on disconnect or when the
refresh token is rejected.

``server.auth: mia_token`` (M51-R1): Mia is the plugin's authorization server. Connecting
records that the user signed in to the plugin's app with Mia (an active grant of the
plugin's own client, ``server.clientId``); :meth:`ConnectionService.headers` then mints a
5-minute access token from that grant for each call, bound to the server URL, with
``openid`` only. Revoking the app in «Подключённые приложения» stops it at once.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast
from urllib.parse import urlsplit

from app.modules.connections.oauth import pkce
from app.modules.connections.oauth.client import (
    InvalidGrantError,
    OAuthClient,
    Registration,
    ServerAuth,
    TokenSet,
)
from app.modules.connections.oauth.errors import (
    OAuthFailedError,
    OAuthProtocolError,
    OAuthUnsupportedError,
    TransientOAuthError,
)
from app.modules.connections.oauth.models import TOKEN_PURPOSE, PluginConnection
from app.modules.models.vault import KeyVault
from app.modules.oauth.service import GrantView, MintedToken
from app.modules.plugins.catalog.models import PluginRelease
from app.modules.plugins.registry.manifest import RemoteAuth
from app.modules.workspace.errors import ConflictError, ResourceNotFoundError, WorkspaceError
from app.modules.workspace.models import Installation, Workspace
from app.modules.workspace.service import MutationResult, WorkspaceContext, WorkspaceService
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

FLOW_PURPOSE: Final = "oauth_flow"
REFRESH_SKEW: Final = timedelta(seconds=60)
CALLBACK_PATH: Final = "/v1/connections/callback"
CLIENT_METADATA_PATH: Final = "/v1/connections/client-metadata.json"
MIA_TOKEN_SCOPES: Final = ("openid",)
"""What a minted plugin token carries (∩ the grant): who the user is, nothing more."""


class MiaTokenIssuer(Protocol):
    """Mia's authorization server, seen from connections (``AuthorizationService``)."""

    async def plugin_grant(self, user_id: uuid.UUID, client_id: str) -> GrantView | None: ...

    async def mint_plugin_token(
        self,
        user_id: uuid.UUID,
        *,
        client_id: str,
        resource: str,
        scopes: tuple[str, ...] = ...,
    ) -> MintedToken | None: ...


class SignInRequiredError(WorkspaceError):
    """``mia_token`` plugin: the user has not signed in to the plugin's app with Mia yet."""

    status_code = 409
    code = "connection_sign_in_required"


@dataclass(frozen=True, slots=True)
class MiaTokenSpec:
    """``server`` of a ``mia_token`` manifest: where the token goes and whose grant mints it."""

    url: str
    client_id: str


def mia_token_spec(manifest: Mapping[str, Any] | None) -> MiaTokenSpec | None:
    server = manifest.get("server") if isinstance(manifest, Mapping) else None
    if (
        not isinstance(server, Mapping)
        or server.get("kind") != "remote"
        or server.get("auth") != "mia_token"
    ):
        return None
    url, client_id = server.get("url"), server.get("clientId")
    if not isinstance(url, str) or not isinstance(client_id, str) or not client_id:
        return None
    return MiaTokenSpec(url=url, client_id=client_id)


@dataclass(frozen=True, slots=True)
class RemoteTarget:
    """The remote MCP endpoint of an installation and the hosts its plugin may reach."""

    url: str
    network: frozenset[str]
    auth: str


def remote_target(installation: Installation) -> RemoteTarget | None:
    """The remote endpoint of an installation, or None for in-process plugins."""
    manifest = installation.manifest
    if manifest is not None:
        server = manifest.get("server")
        if not isinstance(server, Mapping) or server.get("kind") != "remote":
            return None
        permissions = manifest.get("permissions")
        network = permissions.get("network", []) if isinstance(permissions, Mapping) else []
        return RemoteTarget(
            url=str(server["url"]),
            network=frozenset(host for host in network if isinstance(host, str)),
            auth=str(server.get("auth", "none")),
        )
    if installation.custom_url:
        host = (urlsplit(installation.custom_url).hostname or "").lower()
        return RemoteTarget(url=installation.custom_url, network=frozenset({host}), auth="oauth")
    return None


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    id: uuid.UUID
    installation_id: uuid.UUID
    plugin_id: str
    status: str
    account_label: str | None
    scopes: str | None
    connected_at: datetime | None


@dataclass(frozen=True, slots=True)
class CallbackOutcome:
    ok: bool
    installation_id: uuid.UUID | None
    return_to: str | None
    code: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class ConnectionService:
    """Implements the router's ``RemoteCredentials`` and the connection API."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        vault: KeyVault,
        oauth: OAuthClient,
        workspace: WorkspaceService,
        *,
        public_base_url: str,
        flow_ttl_s: int = 600,
        on_change: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] = _now,
        mia_tokens: MiaTokenIssuer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._vault = vault
        self._oauth = oauth
        self._workspace = workspace
        self._base = public_base_url.rstrip("/")
        self._flow_ttl = timedelta(seconds=flow_ttl_s)
        self.on_change = on_change
        self._clock = clock
        self._refresh_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._mia_tokens = mia_tokens

    def use_mia_tokens(self, issuer: MiaTokenIssuer) -> None:
        """Wire Mia's AS (built after the router in the composition) for ``mia_token``."""
        self._mia_tokens = issuer

    @property
    def oauth(self) -> OAuthClient:
        return self._oauth

    @property
    def flow_ttl_seconds(self) -> int:
        return int(self._flow_ttl.total_seconds())

    @property
    def redirect_uri(self) -> str:
        return self._base + CALLBACK_PATH

    @property
    def client_metadata_url(self) -> str | None:
        return self._base + CLIENT_METADATA_PATH if self._base.startswith("https://") else None

    def client_metadata(self) -> dict[str, Any]:
        """The Client ID Metadata Document served at :data:`CLIENT_METADATA_PATH`."""
        return {
            "client_id": self.client_metadata_url or self._base + CLIENT_METADATA_PATH,
            "client_name": "Mia",
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

    # ------------------------------------------------------------------- start

    async def start(
        self,
        ctx: WorkspaceContext,
        installation_id: uuid.UUID,
        *,
        return_to: str | None,
        idempotency_key: str,
        binding: str | None = None,
    ) -> MutationResult:
        """Begin the OAuth flow; ``binding`` ties the callback to the starting browser (web)."""
        async with self._session_factory() as session:
            installation = await _installation(session, ctx, installation_id)
            target = remote_target(installation)
            manifest = await _effective_manifest(session, installation)
        if (target is not None and target.auth == "mia_token") or mia_token_spec(manifest):
            return await self._connect_mia_token(
                ctx, installation_id, installation.plugin_id, manifest, idempotency_key
            )
        if target is None or target.auth != "oauth":
            raise ConflictError("This plugin does not need an account connection.")
        server = await self._discover(target)
        registration = await self._register(server)
        state, verifier = pkce.new_state(), pkce.new_verifier()
        scopes = server.scopes
        authorization_url = self._oauth.authorization_url(
            server.authorization_endpoint,
            client_id=registration.client_id,
            redirect_uri=self.redirect_uri,
            state=state,
            verifier=verifier,
            resource=server.resource,
            scope=scopes,
        )

        async def work(session: AsyncSession) -> MutationResult:
            await _installation(session, ctx, installation_id)  # still ours
            row = await session.scalar(
                select(PluginConnection).where(PluginConnection.installation_id == installation_id)
            )
            if row is None:
                row = PluginConnection(
                    id=uuid.uuid4(),
                    user_id=uuid.UUID(ctx.user_id),
                    workspace_id=ctx.workspace_uuid,
                    installation_id=installation_id,
                    status="pending",
                )
                session.add(row)
            row.status = "pending"
            row.resource = server.resource
            row.issuer = server.issuer
            row.authorization_endpoint = server.authorization_endpoint
            row.token_endpoint = server.token_endpoint
            row.revocation_endpoint = server.revocation_endpoint
            row.client_id = registration.client_id
            row.auth_method = registration.auth_method
            row.scopes = scopes
            row.account_label = urlsplit(server.issuer).hostname
            row.connected_at = None
            row.expires_at = None
            row.flow_state_hash = pkce.hash_state(state)
            row.flow_expires_at = self._clock() + self._flow_ttl
            row.flow_return_to = return_to
            row.flow_binding_hash = pkce.hash_state(binding) if binding else None
            await session.flush()
            secrets: dict[str, str] = {}
            if registration.client_secret is not None:
                secrets["client_secret"] = registration.client_secret
            row.secret_ciphertext = await self._vault.seal_secret(
                session,
                row.user_id,
                json.dumps(secrets).encode(),
                purpose=TOKEN_PURPOSE,
                record_id=row.id,
            )
            row.flow_verifier_ciphertext = await self._vault.seal_secret(
                session,
                row.user_id,
                verifier.encode(),
                purpose=FLOW_PURPOSE,
                record_id=row.id,
            )
            await session.execute(
                update(Installation)
                .where(
                    Installation.id == installation_id,
                    Installation.workspace_id == ctx.workspace_uuid,
                    Installation.status == "active",
                )
                .values(
                    status="needs_connection",
                    state_version=Installation.state_version + 1,
                    updated_at=func.now(),
                )
            )
            return MutationResult(
                201,
                {"connection_id": str(row.id), "authorization_url": authorization_url},
            )

        return await self._workspace.mutate(
            ctx,
            idempotency_key,
            f"start-connection:{installation_id}:{return_to or ''}",
            work,
        )

    async def _connect_mia_token(
        self,
        ctx: WorkspaceContext,
        installation_id: uuid.UUID,
        plugin_id: str,
        manifest: Mapping[str, Any] | None,
        idempotency_key: str,
    ) -> MutationResult:
        """``mia_token``: no browser step; the user's grant of the plugin's client is the link.

        200 ``{connection_id, status: "connected"}`` and the installation becomes active;
        409 ``connection_sign_in_required`` (``details.client_id``) when the user has not
        signed in to the plugin's app with Mia (or that grant lacks ``openid``).
        """
        spec = mia_token_spec(manifest)
        if spec is None or self._mia_tokens is None:
            raise ConflictError("This plugin cannot be connected with a Mia token.")
        owner = uuid.UUID(ctx.user_id)
        grant = await self._mia_tokens.plugin_grant(owner, spec.client_id)
        if grant is None or not set(MIA_TOKEN_SCOPES) <= set(grant.scopes):
            raise SignInRequiredError(
                "Sign in to this app with Mia first, then connect it again.",
                details={"client_id": spec.client_id},
            )
        label = (grant.client_name or urlsplit(spec.client_id).hostname or "")[:200]

        async def work(session: AsyncSession) -> MutationResult:
            await _installation(session, ctx, installation_id)  # still ours
            row = await session.scalar(
                select(PluginConnection).where(PluginConnection.installation_id == installation_id)
            )
            if row is None:
                row = PluginConnection(
                    id=uuid.uuid4(),
                    user_id=owner,
                    workspace_id=ctx.workspace_uuid,
                    installation_id=installation_id,
                    status="connected",
                )
                session.add(row)
            row.status = "connected"
            row.resource = spec.url
            row.issuer = None
            row.authorization_endpoint = None
            row.token_endpoint = None
            row.revocation_endpoint = None
            row.client_id = spec.client_id
            row.auth_method = "mia_token"
            row.scopes = " ".join(MIA_TOKEN_SCOPES)
            row.account_label = label
            row.secret_ciphertext = None
            row.expires_at = None
            row.connected_at = self._clock()
            row.flow_state_hash = None
            row.flow_verifier_ciphertext = None
            row.flow_expires_at = None
            row.flow_return_to = None
            row.flow_binding_hash = None
            await session.flush()
            await session.execute(
                update(Installation)
                .where(
                    Installation.id == installation_id,
                    Installation.workspace_id == ctx.workspace_uuid,
                    Installation.status == "needs_connection",
                    Installation.tools_digest.is_not(None),
                )
                .values(
                    status="active",
                    state_version=Installation.state_version + 1,
                    updated_at=func.now(),
                )
            )
            return MutationResult(200, {"connection_id": str(row.id), "status": "connected"})

        result = await self._workspace.mutate(
            ctx, idempotency_key, f"connect-mia-token:{installation_id}", work
        )
        if self.on_change is not None:
            self.on_change(plugin_id)
        return result

    async def _discover(self, target: RemoteTarget) -> ServerAuth:
        try:
            return await self._oauth.discover(target.url, target.network)
        except OAuthProtocolError as error:
            if error.unsupported:
                raise OAuthUnsupportedError(
                    "This server's sign-in is not supported by Mia."
                ) from error
            logger.warning("oauth_discovery_failed", extra={"reason": str(error)})
            raise OAuthFailedError("The server's sign-in could not be discovered.") from error
        except TransientOAuthError as error:
            raise OAuthFailedError("The server's sign-in is unreachable.") from error

    async def _register(self, server: ServerAuth) -> Registration:
        try:
            return await self._oauth.register(
                server, redirect_uri=self.redirect_uri, cimd_url=self.client_metadata_url
            )
        except OAuthProtocolError as error:
            if error.unsupported:
                raise OAuthUnsupportedError(
                    "This server needs a registered client, which Mia does not have."
                ) from error
            logger.warning("oauth_registration_failed", extra={"reason": str(error)})
            raise OAuthFailedError("Mia could not register with the server.") from error
        except TransientOAuthError as error:
            raise OAuthFailedError("The server's sign-in is unreachable.") from error

    # ---------------------------------------------------------------- callback

    async def complete(
        self,
        *,
        state: str | None,
        code: str | None,
        error: str | None,
        binding: str | None = None,
    ) -> CallbackOutcome:
        """Finish a flow from the browser redirect; the single-use ``state`` is the credential.

        A flow started by a browser carries a binding hash: the callback must present the
        matching value (a short-lived Lax cookie), so a link prepared by someone else cannot
        attach their provider account to this user (login CSRF, OI-M15-003).
        """
        if not state:
            return CallbackOutcome(False, None, None, "connection_flow_invalid")
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(PluginConnection)
                .where(PluginConnection.flow_state_hash == pkce.hash_state(state))
                .with_for_update()
            )
            if row is None or row.flow_expires_at is None or row.flow_expires_at <= self._clock():
                if row is not None:
                    row.flow_state_hash = None
                    row.flow_verifier_ciphertext = None
                return CallbackOutcome(False, None, None, "connection_flow_invalid")
            verifier = (
                await self._vault.open_secret(
                    session,
                    row.user_id,
                    cast(bytes, row.flow_verifier_ciphertext),
                    purpose=FLOW_PURPOSE,
                    record_id=row.id,
                )
            ).decode()
            secrets = await self._secrets(session, row)
            snapshot = _Snapshot.of(row)
            return_to = row.flow_return_to
            installation_id = row.installation_id
            expected_binding = row.flow_binding_hash
            # Single use: the state is burnt before the code is exchanged.
            row.flow_state_hash = None
            row.flow_verifier_ciphertext = None
            row.flow_expires_at = None
            row.flow_return_to = None
            row.flow_binding_hash = None
        if expected_binding is not None and not (
            binding and hmac.compare_digest(pkce.hash_state(binding), expected_binding)
        ):
            return CallbackOutcome(False, installation_id, return_to, "connection_flow_invalid")
        if error or not code:
            return CallbackOutcome(False, installation_id, return_to, "connection_oauth_failed")
        try:
            tokens = await self._oauth.exchange_code(
                token_endpoint=snapshot.token_endpoint,
                code=code,
                verifier=verifier,
                redirect_uri=self.redirect_uri,
                resource=snapshot.resource,
                client_id=snapshot.client_id,
                client_secret=secrets.get("client_secret"),
                auth_method=snapshot.auth_method,
            )
        except (OAuthProtocolError, InvalidGrantError, TransientOAuthError) as failure:
            logger.warning("oauth_code_exchange_failed", extra={"kind": type(failure).__name__})
            return CallbackOutcome(False, installation_id, return_to, "connection_oauth_failed")
        plugin_id = await self._store_tokens(snapshot.id, tokens, secrets)
        if plugin_id is not None and self.on_change is not None:
            self.on_change(plugin_id)
        return CallbackOutcome(True, installation_id, return_to)

    async def _store_tokens(
        self, connection_id: uuid.UUID, tokens: TokenSet, secrets: dict[str, str]
    ) -> str | None:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(PluginConnection)
                .where(PluginConnection.id == connection_id)
                .with_for_update()
            )
            if row is None:
                return None
            await self._seal(session, row, tokens, secrets)
            row.status = "connected"
            row.connected_at = self._clock()
            if tokens.scope:
                row.scopes = tokens.scope
            plugin_id = await session.scalar(
                update(Installation)
                .where(
                    Installation.id == row.installation_id,
                    Installation.workspace_id == row.workspace_id,
                    Installation.status == "needs_connection",
                    Installation.tools_digest.is_not(None),
                )
                .values(
                    status="active",
                    state_version=Installation.state_version + 1,
                    updated_at=func.now(),
                )
                .returning(Installation.plugin_id)
            )
            if plugin_id is None:
                plugin_id = await session.scalar(
                    select(Installation.plugin_id).where(Installation.id == row.installation_id)
                )
            return plugin_id

    async def _seal(
        self,
        session: AsyncSession,
        row: PluginConnection,
        tokens: TokenSet,
        secrets: dict[str, str],
    ) -> None:
        body = dict(secrets)
        body["access_token"] = tokens.access_token
        if tokens.refresh_token is not None:
            body["refresh_token"] = tokens.refresh_token
        else:
            body.pop("refresh_token", None)
        row.secret_ciphertext = await self._vault.seal_secret(
            session,
            row.user_id,
            json.dumps(body).encode(),
            purpose=TOKEN_PURPOSE,
            record_id=row.id,
        )
        row.expires_at = (
            datetime.fromtimestamp(tokens.expires_at, UTC)
            if tokens.expires_at is not None
            else None
        )

    async def _secrets(self, session: AsyncSession, row: PluginConnection) -> dict[str, str]:
        if row.secret_ciphertext is None:
            return {}
        raw = await self._vault.open_secret(
            session,
            row.user_id,
            row.secret_ciphertext,
            purpose=TOKEN_PURPOSE,
            record_id=row.id,
        )
        data = json.loads(raw)
        return {key: value for key, value in data.items() if isinstance(value, str)}

    # -------------------------------------------------- RemoteCredentials (M13)

    async def headers(
        self, *, user_id: str, installation_id: str, plugin_id: str, auth: RemoteAuth
    ) -> Mapping[str, str] | None:
        """``Authorization`` for this user's connection; None when not (or no longer) connected."""
        if auth not in ("oauth", "mia_token"):
            return None
        try:
            owner, installation = uuid.UUID(user_id), uuid.UUID(installation_id)
        except ValueError:
            return None
        if auth == "mia_token":
            return await self._mia_token_headers(owner, installation)
        async with self._session_factory() as session:
            row = await session.scalar(
                select(PluginConnection).where(
                    PluginConnection.installation_id == installation,
                    PluginConnection.user_id == owner,
                    PluginConnection.status == "connected",
                )
            )
            if row is None or row.secret_ciphertext is None:
                return None
            connection_id = row.id
            stale = row.expires_at is not None and row.expires_at <= self._clock() + REFRESH_SKEW
            secrets = await self._secrets(session, row)
        if stale:
            secrets = await self._refresh(connection_id, plugin_id) or {}
        token = secrets.get("access_token")
        return {"Authorization": f"Bearer {token}"} if token else None

    async def _mia_token_headers(
        self, owner: uuid.UUID, installation_id: uuid.UUID
    ) -> Mapping[str, str] | None:
        """Mint a token for one call; None (→ ``connection_required``) without a live grant.

        The connection must exist and still name the manifest's server URL and client: a
        release that moves the server or changes ``clientId`` needs a new connect.
        """
        if self._mia_tokens is None:
            return None
        async with self._session_factory() as session:
            found = (
                await session.execute(
                    select(PluginConnection, Installation)
                    .join(
                        Installation,
                        (Installation.id == PluginConnection.installation_id)
                        & (Installation.workspace_id == PluginConnection.workspace_id),
                    )
                    .join(Workspace, Workspace.id == Installation.workspace_id)
                    .where(
                        PluginConnection.installation_id == installation_id,
                        PluginConnection.user_id == owner,
                        PluginConnection.status == "connected",
                        PluginConnection.auth_method == "mia_token",
                        Workspace.owner_user_id == owner,
                    )
                )
            ).first()
            if found is None:
                return None
            row, installation = found
            spec = mia_token_spec(await _effective_manifest(session, installation))
        if spec is None or row.client_id != spec.client_id or row.resource != spec.url:
            return None
        minted = await self._mia_tokens.mint_plugin_token(
            owner, client_id=spec.client_id, resource=spec.url, scopes=MIA_TOKEN_SCOPES
        )
        if minted is None:
            logger.info("mia_token_grant_missing", extra={"installation_id": str(installation_id)})
            return None
        return {"Authorization": f"Bearer {minted.access_token}"}

    async def _refresh(self, connection_id: uuid.UUID, plugin_id: str) -> dict[str, str] | None:
        lock = self._refresh_locks.setdefault(connection_id, asyncio.Lock())
        async with lock, self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(PluginConnection)
                .where(PluginConnection.id == connection_id)
                .with_for_update()
            )
            if row is None or row.status != "connected" or row.secret_ciphertext is None:
                return None
            secrets = await self._secrets(session, row)
            if row.expires_at is None or row.expires_at > self._clock() + REFRESH_SKEW:
                return secrets  # another worker refreshed while we waited
            refresh_token = secrets.get("refresh_token")
            if refresh_token is None or row.token_endpoint is None or row.client_id is None:
                await self._mark_expired(session, row)
                return None
            try:
                tokens = await self._oauth.refresh(
                    token_endpoint=row.token_endpoint,
                    refresh_token=refresh_token,
                    resource=cast(str, row.resource),
                    scope=None,
                    client_id=row.client_id,
                    client_secret=secrets.get("client_secret"),
                    auth_method=row.auth_method or "none",
                )
            except InvalidGrantError:
                await self._mark_expired(session, row)
                logger.info("oauth_refresh_rejected", extra={"plugin_id": plugin_id})
                return None
            except OAuthProtocolError as error:
                logger.warning("oauth_refresh_failed", extra={"reason": str(error)})
                raise TransientOAuthError("refresh failed") from error
            if tokens.refresh_token is None:
                tokens = TokenSet(
                    tokens.access_token, refresh_token, tokens.expires_at, tokens.scope
                )
            await self._seal(session, row, tokens, secrets)
            return await self._secrets(session, row)

    async def _mark_expired(self, session: AsyncSession, row: PluginConnection) -> None:
        row.status = "expired"
        row.secret_ciphertext = None
        row.expires_at = None
        await session.execute(
            update(Installation)
            .where(
                Installation.id == row.installation_id,
                Installation.workspace_id == row.workspace_id,
                Installation.status == "active",
            )
            .values(
                status="needs_connection",
                state_version=Installation.state_version + 1,
                updated_at=func.now(),
            )
        )

    # ------------------------------------------------------------------- read

    async def list_connections(self, ctx: WorkspaceContext) -> list[ConnectionInfo]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(PluginConnection, Installation.plugin_id)
                .join(
                    Installation,
                    (Installation.id == PluginConnection.installation_id)
                    & (Installation.workspace_id == PluginConnection.workspace_id),
                )
                .where(PluginConnection.workspace_id == ctx.workspace_uuid)
                .order_by(PluginConnection.created_at, PluginConnection.id)
            )
            return [_info(row, plugin_id) for row, plugin_id in rows]

    # ----------------------------------------------------------------- revoke

    async def disconnect(
        self, ctx: WorkspaceContext, connection_id: uuid.UUID, *, idempotency_key: str
    ) -> MutationResult:
        """``DELETE /v1/connections/{id}``: revoke upstream, delete the tokens."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(PluginConnection).where(
                    PluginConnection.id == connection_id,
                    PluginConnection.workspace_id == ctx.workspace_uuid,
                )
            )
            if row is None:
                raise ResourceNotFoundError("The connection was not found.")
            installation_id = row.installation_id
        await self.revoke_installation(ctx, installation_id)

        async def work(session: AsyncSession) -> MutationResult:
            await session.execute(
                update(Installation)
                .where(
                    Installation.id == installation_id,
                    Installation.workspace_id == ctx.workspace_uuid,
                    Installation.status == "active",
                )
                .values(
                    status="needs_connection",
                    state_version=Installation.state_version + 1,
                    updated_at=func.now(),
                )
            )
            return MutationResult(204, None)

        return await self._workspace.mutate(
            ctx, idempotency_key, f"disconnect:{connection_id}", work
        )

    async def revoke_installation(self, ctx: WorkspaceContext, installation_id: uuid.UUID) -> bool:
        """Revoke at the authorization server (best effort) and delete the connection row.

        Returns True when a connection existed. Called before an installation is removed, so
        uninstalling never leaves a usable refresh token behind at the provider.
        """
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(PluginConnection)
                .where(
                    PluginConnection.installation_id == installation_id,
                    PluginConnection.workspace_id == ctx.workspace_uuid,
                )
                .with_for_update()
            )
            if row is None:
                return False
            secrets = await self._secrets(session, row)
            snapshot = _Snapshot.of(row)
            await session.delete(row)
        await self._revoke_upstream(snapshot, secrets)
        return True

    async def _revoke_upstream(self, snapshot: _Snapshot, secrets: Mapping[str, str]) -> None:
        endpoint = snapshot.revocation_endpoint
        tokens = [
            (secrets[name], hint)
            for name, hint in (("refresh_token", "refresh_token"), ("access_token", "access_token"))
            if name in secrets
        ]
        if endpoint is None or not tokens or snapshot.client_id == "":
            logger.info("oauth_revocation_skipped", extra={"has_endpoint": endpoint is not None})
            return
        try:
            accepted = await self._oauth.revoke(
                revocation_endpoint=endpoint,
                tokens=tokens,
                client_id=snapshot.client_id,
                client_secret=secrets.get("client_secret"),
                auth_method=snapshot.auth_method,
            )
        except Exception as error:  # revocation is best effort; the local tokens are gone anyway
            logger.warning("oauth_revocation_failed", extra={"error_type": type(error).__name__})
            return
        logger.info("oauth_revoked", extra={"accepted": accepted})


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """Connection fields needed after the transaction that read them has ended."""

    id: uuid.UUID
    token_endpoint: str
    revocation_endpoint: str | None
    resource: str
    client_id: str
    auth_method: str

    @classmethod
    def of(cls, row: PluginConnection) -> _Snapshot:
        return cls(
            id=row.id,
            token_endpoint=row.token_endpoint or "",
            revocation_endpoint=row.revocation_endpoint,
            resource=row.resource or "",
            client_id=row.client_id or "",
            auth_method=row.auth_method or "none",
        )


def _info(row: PluginConnection, plugin_id: str) -> ConnectionInfo:
    return ConnectionInfo(
        id=row.id,
        installation_id=row.installation_id,
        plugin_id=plugin_id,
        status=row.status,
        account_label=row.account_label,
        scopes=row.scopes,
        connected_at=row.connected_at,
    )


async def _effective_manifest(
    session: AsyncSession, installation: Installation
) -> Mapping[str, Any] | None:
    """The installation's manifest, or its release's when the row does not carry one."""
    if installation.manifest is not None:
        return installation.manifest
    if installation.source != "catalog":
        return None
    return cast(
        "Mapping[str, Any] | None",
        await session.scalar(
            select(PluginRelease.manifest).where(
                PluginRelease.plugin_id == installation.plugin_id,
                PluginRelease.version == installation.version,
            )
        ),
    )


async def _installation(
    session: AsyncSession, ctx: WorkspaceContext, installation_id: uuid.UUID
) -> Installation:
    row = await session.scalar(
        select(Installation).where(
            Installation.id == installation_id, Installation.workspace_id == ctx.workspace_uuid
        )
    )
    if row is None:
        raise ResourceNotFoundError("The installation was not found.")
    return row
