"""OAuth 2.1 authorization server of «Войти через Mia» (ADR-0013).

Flow: ``/oauth/authorize`` validates the client (CIMD) and its exact redirect URI,
stores a pending request and sends the browser to the consent page; the signed-in
user approves (scopes, memory categories, monthly limit) and receives a single-use
code (60 s, bound to client, redirect URI and the PKCE S256 challenge); ``/oauth/token``
redeems it for a short-lived access token and a rotating refresh token. Every
credential is stored as a SHA-256 digest only.

Security rules kept here:

- PKCE S256 only, the verifier is checked in constant time;
- redirect URIs are compared as exact strings, at authorize and at token time;
- a code is redeemed at most once; a replay revokes what the code produced;
- a refresh token is used at most once; a replay revokes the whole rotation family;
- effective scopes are re-intersected with the live grant on every request;
- RFC 8707 resources are Mia's own API and ``/mcp``, ``OAUTH_EXTRA_RESOURCES`` and what the
  :class:`ResourcePolicy` knows (plugin servers, M51-R1); anything else is ``invalid_target``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Protocol, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.memory_provider import MemoryCategory
from app.modules.memory.domain import CATEGORIES, SENSITIVE_CATEGORIES
from app.modules.oauth import secrets as creds
from app.modules.oauth.cimd import ClientMetadataError, ClientMetadataResolver
from app.modules.oauth.errors import AppApiError, AuthorizeError, TokenEndpointError, app_error
from app.modules.oauth.idtoken import IdTokenSigner
from app.modules.oauth.models import (
    OAuthAuthorizationRequest,
    OAuthCode,
    OAuthGrant,
    OAuthToken,
)
from app.modules.oauth.scopes import (
    DEFAULT_CONTEXT_CATEGORIES,
    InvalidScopeError,
    ScopeText,
    describe,
    parse_scope,
)
from app.modules.users.models import User

logger = logging.getLogger(__name__)
MAX_STATE: Final = 1024
MAX_NONCE: Final = 512


def utc_now() -> datetime:
    return datetime.now(UTC)


def _split(scopes: str) -> tuple[str, ...]:
    return tuple(s for s in scopes.split(" ") if s)


def _join(scopes: Sequence[str] | frozenset[str]) -> str:
    return " ".join(scopes)


def with_query(uri: str, params: Mapping[str, str]) -> str:
    """Append parameters to a redirect URI, keeping its own query."""
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    issuer: str
    consent_url: str
    access_ttl_s: int = 900
    refresh_ttl_s: int = 30 * 86400
    code_ttl_s: int = 60
    request_ttl_s: int = 600
    id_token_ttl_s: int = 3600
    default_monthly_limit_usd: Decimal = Decimal("5.00")
    max_monthly_limit_usd: Decimal = Decimal("100.00")
    extra_resources: frozenset[str] = frozenset()
    """``OAUTH_EXTRA_RESOURCES``: plugin servers any client may ask for (dev/ops override)."""
    plugin_token_ttl_s: int = 300
    """Lifetime of a token minted for a ``mia_token`` plugin call (never above 5 minutes)."""

    @property
    def mcp_resource(self) -> str:
        return self.issuer.rstrip("/") + "/mcp"

    @property
    def own_resources(self) -> frozenset[str]:
        """Mia's own resources: the API origin and the MCP endpoint."""
        base = self.issuer.rstrip("/")
        return frozenset({base, base + "/", self.mcp_resource})

    @property
    def resources(self) -> frozenset[str]:
        """Statically accepted RFC 8707 resource indicators (own + configured extras)."""
        return self.own_resources | self.extra_resources


class ResourcePolicy(Protocol):
    """Which plugin servers (RFC 8707 resources) a client may get a token for (M51-R1)."""

    async def allows(self, resource: str, *, client_id: str) -> bool: ...


PLUGIN_TOKEN_MAX_TTL_S: Final = 300


@dataclass(frozen=True, slots=True)
class AuthorizeParams:
    client_id: str
    redirect_uri: str
    response_type: str
    scope: str
    state: str
    code_challenge: str
    code_challenge_method: str
    resource: str | None = None
    nonce: str | None = None


@dataclass(frozen=True, slots=True)
class AppAccess:
    """A verified app access token: who, which client, which (live) scopes."""

    token_id: uuid.UUID
    grant_id: uuid.UUID
    user_id: uuid.UUID
    client_id: str
    client_name: str
    scopes: frozenset[str]
    memory_categories: tuple[MemoryCategory, ...]
    monthly_limit_usd: Decimal
    resource: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A short-lived access token minted by Mia itself for a ``mia_token`` plugin call."""

    access_token: str
    scopes: tuple[str, ...]
    resource: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RequestView:
    client_id: str
    client_name: str
    client_uri: str | None
    logo_uri: str | None
    redirect_host: str
    scopes: tuple[ScopeText, ...]
    memory_categories: tuple[MemoryCategory, ...]
    monthly_limit_usd: Decimal
    max_monthly_limit_usd: Decimal
    expires_at: datetime
    existing_grant: bool


@dataclass(frozen=True, slots=True)
class Decision:
    approve: bool
    scopes: tuple[str, ...] | None = None
    memory_categories: tuple[MemoryCategory, ...] | None = None
    monthly_limit_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
    id_token: str | None = None

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
        }
        if self.id_token is not None:
            body["id_token"] = self.id_token
        return body


@dataclass(frozen=True, slots=True)
class GrantView:
    id: uuid.UUID
    client_id: str
    client_name: str
    client_uri: str | None
    logo_uri: str | None
    scopes: tuple[str, ...]
    memory_categories: tuple[MemoryCategory, ...]
    monthly_limit_usd: Decimal
    created_at: datetime
    updated_at: datetime


def _grant_view(row: OAuthGrant) -> GrantView:
    return GrantView(
        id=row.id,
        client_id=row.client_id,
        client_name=row.client_name,
        client_uri=row.client_uri,
        logo_uri=row.logo_uri,
        scopes=_split(row.scopes),
        memory_categories=_categories(row.memory_categories),
        monthly_limit_usd=row.monthly_limit_usd,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _categories(value: str) -> tuple[MemoryCategory, ...]:
    return tuple(cast(MemoryCategory, c) for c in _split(value) if c in CATEGORIES)


def _redirect_host(uri: str) -> str:
    parts = urlsplit(uri)
    return parts.netloc or parts.scheme


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class AuthorizationService:
    sessions: async_sessionmaker[AsyncSession]
    clients: ClientMetadataResolver
    signer: IdTokenSigner
    config: OAuthConfig
    subject_key: bytes
    clock: Callable[[], datetime] = field(default=utc_now)
    resources: ResourcePolicy | None = None
    """Plugin servers accepted as RFC 8707 resources besides :attr:`OAuthConfig.resources`."""
    purge_every_s: float = 600.0
    _purged_at: float = field(default=0.0, init=False, repr=False)

    async def purge_expired(self) -> None:
        """Drop requests, codes and tokens expired for more than a day (bounded tables)."""
        cutoff = self.clock() - timedelta(days=1)
        async with self.sessions() as session, session.begin():
            for model in (OAuthAuthorizationRequest, OAuthCode, OAuthToken):
                await session.execute(delete(model).where(model.expires_at < cutoff))

    async def _maybe_purge(self) -> None:
        now = time.monotonic()
        if now - self._purged_at < self.purge_every_s:
            return
        self._purged_at = now
        try:
            await self.purge_expired()
        except Exception:
            logger.warning("oauth_purge_failed")

    # -- identity ----------------------------------------------------------------

    def subject(self, user_id: uuid.UUID, client_id: str) -> str:
        """Pairwise ``sub`` (OIDC §8.1): stable per user and client host, unlinkable across."""
        sector = (urlsplit(client_id).hostname or client_id).lower()
        mac = hmac.new(self.subject_key, f"{sector}\n{user_id}".encode(), hashlib.sha256)
        return base64.urlsafe_b64encode(mac.digest()[:24]).decode().rstrip("=")

    def claims(self, user: User, *, client_id: str, scopes: frozenset[str]) -> dict[str, Any]:
        body: dict[str, Any] = {"sub": self.subject(user.id, client_id)}
        if "profile" in scopes:
            body["name"] = user.display_name or ""
            if user.avatar_url:
                body["picture"] = user.avatar_url
            body["locale"] = user.locale
            body["zoneinfo"] = user.timezone
        if "email" in scopes:
            body["email"] = user.email
            body["email_verified"] = bool(user.email_verified)
        return body

    # -- authorize -----------------------------------------------------------------

    async def authorize(self, params: AuthorizeParams) -> str:
        """Validate ``/oauth/authorize`` and return the consent page URL."""
        if not params.client_id:
            raise AuthorizeError("invalid_request", "client_id is required.")
        try:
            client = await self.clients.resolve(params.client_id)
        except ClientMetadataError as error:
            raise AuthorizeError(
                "invalid_client", f"Client metadata rejected: {error.reason}."
            ) from None
        if not any(creds.same(params.redirect_uri, uri) for uri in client.redirect_uris):
            raise AuthorizeError("invalid_request", "redirect_uri is not registered.")
        state = params.state

        def fail(error: str, description: str) -> AuthorizeError:
            return AuthorizeError(
                error, description, redirect_uri=params.redirect_uri, state=state or None
            )

        if params.response_type != "code":
            raise fail("unsupported_response_type", "Only response_type=code is supported.")
        if not state or len(state) > MAX_STATE:
            raise fail("invalid_request", "state is required.")
        if params.code_challenge_method != "S256" or not creds.valid_challenge(
            params.code_challenge
        ):
            raise fail("invalid_request", "PKCE with code_challenge_method=S256 is required.")
        try:
            scopes = parse_scope(params.scope)
        except InvalidScopeError:
            raise fail("invalid_scope", "The requested scope is invalid.") from None
        if params.resource is not None and not await self.resource_allowed(
            params.resource, client_id=client.client_id
        ):
            raise fail("invalid_target", "Unknown resource.")
        if params.nonce is not None and len(params.nonce) > MAX_NONCE:
            raise fail("invalid_request", "nonce is too long.")
        await self._maybe_purge()
        handle = creds.new_secret("request")
        now = self.clock()
        async with self.sessions() as session, session.begin():
            session.add(
                OAuthAuthorizationRequest(
                    id=uuid.uuid4(),
                    handle_hash=creds.digest(handle),
                    client_id=client.client_id,
                    client_name=client.client_name,
                    client_uri=client.client_uri,
                    logo_uri=client.logo_uri,
                    redirect_uri=params.redirect_uri,
                    scopes=_join(scopes),
                    state=state,
                    code_challenge=params.code_challenge,
                    resource=params.resource,
                    nonce=params.nonce,
                    status="pending",
                    created_at=now,
                    expires_at=now + timedelta(seconds=self.config.request_ttl_s),
                )
            )
        return with_query(self.config.consent_url, {"request": handle})

    async def resource_allowed(self, resource: str, *, client_id: str) -> bool:
        """RFC 8707: Mia's own resources, configured extras, or a plugin server the policy knows."""
        if resource in self.config.resources:
            return True
        if self.resources is None or len(resource) > 2048:
            return False
        try:
            return await self.resources.allows(resource, client_id=client_id)
        except Exception:
            logger.warning("oauth_resource_policy_failed")
            return False

    async def _request_for(
        self, session: AsyncSession, handle: str, user_id: uuid.UUID, *, lock: bool
    ) -> OAuthAuthorizationRequest:
        if not creds.looks_like(handle, "request"):
            raise app_error("oauth_request_not_found")
        statement = select(OAuthAuthorizationRequest).where(
            OAuthAuthorizationRequest.handle_hash == creds.digest(handle)
        )
        if lock:
            statement = statement.with_for_update()
        row = (await session.scalars(statement)).first()
        if row is None or (row.user_id is not None and row.user_id != user_id):
            raise app_error("oauth_request_not_found")
        return row

    async def _active_grant(
        self, session: AsyncSession, user_id: uuid.UUID, client_id: str, *, lock: bool = False
    ) -> OAuthGrant | None:
        statement = select(OAuthGrant).where(
            OAuthGrant.user_id == user_id,
            OAuthGrant.client_id == client_id,
            OAuthGrant.revoked_at.is_(None),
        )
        if lock:
            statement = statement.with_for_update()
        return (await session.scalars(statement)).first()

    async def view_request(self, handle: str, user_id: uuid.UUID) -> RequestView:
        """What the consent page shows; the first signed-in viewer owns the request."""
        async with self.sessions() as session, session.begin():
            row = await self._request_for(session, handle, user_id, lock=True)
            if row.status != "pending":
                raise app_error("oauth_request_decided")
            if row.expires_at <= self.clock():
                raise app_error("oauth_request_expired")
            row.user_id = user_id
            grant = await self._active_grant(session, user_id, row.client_id)
            categories = (
                _categories(grant.memory_categories) if grant else DEFAULT_CONTEXT_CATEGORIES
            )
            return RequestView(
                client_id=row.client_id,
                client_name=row.client_name,
                client_uri=row.client_uri,
                logo_uri=row.logo_uri,
                redirect_host=_redirect_host(row.redirect_uri),
                scopes=tuple(describe(s) for s in _split(row.scopes)),
                memory_categories=categories,
                monthly_limit_usd=(
                    grant.monthly_limit_usd if grant else self.config.default_monthly_limit_usd
                ),
                max_monthly_limit_usd=self.config.max_monthly_limit_usd,
                expires_at=row.expires_at,
                existing_grant=grant is not None,
            )

    async def decide(
        self, handle: str, user_id: uuid.UUID, decision: Decision, *, idempotency_key: str
    ) -> str:
        """Record the user's answer; returns where the browser goes next."""
        key_hash = creds.digest(idempotency_key)
        now = self.clock()
        async with self.sessions() as session, session.begin():
            row = await self._request_for(session, handle, user_id, lock=True)
            if row.status != "pending":
                if (
                    row.decision_key_hash is not None
                    and creds.same(row.decision_key_hash, key_hash)
                    and row.redirect_to is not None
                ):
                    return row.redirect_to
                raise app_error("oauth_request_decided")
            if row.user_id is None:
                # The page must have shown the request first (binds it to this user).
                raise app_error("oauth_request_not_found")
            if row.expires_at <= now:
                raise app_error("oauth_request_expired")
            base = {"iss": self.config.issuer}
            if not decision.approve:
                row.status = "denied"
                row.redirect_to = with_query(
                    row.redirect_uri, {"error": "access_denied", "state": row.state, **base}
                )
                row.decision_key_hash = key_hash
                return row.redirect_to
            requested = _split(row.scopes)
            chosen = decision.scopes if decision.scopes is not None else requested
            if not chosen or not set(chosen) <= set(requested):
                raise AppApiError(422, "validation_error", "The chosen scopes are invalid.")
            scopes = tuple(s for s in requested if s in set(chosen))
            categories = tuple(
                c
                for c in (
                    decision.memory_categories
                    if decision.memory_categories is not None
                    else DEFAULT_CONTEXT_CATEGORIES
                )
                if c in CATEGORIES and c not in SENSITIVE_CATEGORIES
            )
            limit = decision.monthly_limit_usd
            if limit is None:
                limit = self.config.default_monthly_limit_usd
            limit = _money(min(max(limit, Decimal(0)), self.config.max_monthly_limit_usd))
            grant = await self._active_grant(session, user_id, row.client_id, lock=True)
            if grant is None:
                grant = OAuthGrant(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    client_id=row.client_id,
                    created_at=now,
                    client_name=row.client_name,
                    scopes="",
                    monthly_limit_usd=limit,
                )
                session.add(grant)
            grant.client_name = row.client_name
            grant.client_uri = row.client_uri
            grant.logo_uri = row.logo_uri
            grant.scopes = _join(scopes)
            grant.memory_categories = _join(categories)
            grant.monthly_limit_usd = limit
            grant.updated_at = now
            await session.flush()
            code = creds.new_secret("code")
            session.add(
                OAuthCode(
                    id=uuid.uuid4(),
                    code_hash=creds.digest(code),
                    grant_id=grant.id,
                    request_id=row.id,
                    user_id=user_id,
                    client_id=row.client_id,
                    redirect_uri=row.redirect_uri,
                    code_challenge=row.code_challenge,
                    scopes=_join(scopes),
                    resource=row.resource,
                    nonce=row.nonce,
                    auth_time=now,
                    created_at=now,
                    expires_at=now + timedelta(seconds=self.config.code_ttl_s),
                )
            )
            row.status = "approved"
            row.decision_key_hash = key_hash
            row.redirect_to = with_query(
                row.redirect_uri, {"code": code, "state": row.state, **base}
            )
            return row.redirect_to

    # -- token endpoint ------------------------------------------------------------

    async def _issue(
        self,
        session: AsyncSession,
        *,
        grant: OAuthGrant,
        scopes: tuple[str, ...],
        resource: str | None,
        family_id: uuid.UUID,
        code_id: uuid.UUID | None,
        nonce: str | None = None,
        auth_time: datetime | None = None,
    ) -> TokenResponse:
        now = self.clock()
        access = creds.new_secret("access")
        refresh = creds.new_secret("refresh")
        common = {
            "grant_id": grant.id,
            "family_id": family_id,
            "code_id": code_id,
            "user_id": grant.user_id,
            "client_id": grant.client_id,
            "scopes": _join(scopes),
            "resource": resource,
            "created_at": now,
        }
        session.add(
            OAuthToken(
                id=uuid.uuid4(),
                token_hash=creds.digest(access),
                kind="access",
                expires_at=now + timedelta(seconds=self.config.access_ttl_s),
                **common,
            )
        )
        session.add(
            OAuthToken(
                id=uuid.uuid4(),
                token_hash=creds.digest(refresh),
                kind="refresh",
                expires_at=now + timedelta(seconds=self.config.refresh_ttl_s),
                **common,
            )
        )
        id_token = None
        if "openid" in scopes:
            user = await session.get(User, grant.user_id)
            if user is not None:
                extra = self.claims(user, client_id=grant.client_id, scopes=frozenset(scopes))
                subject = extra.pop("sub")
                id_token = self.signer.id_token(
                    issuer=self.config.issuer,
                    subject=subject,
                    audience=grant.client_id,
                    auth_time=int((auth_time or now).timestamp()),
                    nonce=nonce,
                    ttl_s=self.config.id_token_ttl_s,
                    extra=extra,
                    now=now.timestamp(),
                )
        return TokenResponse(
            access_token=access,
            refresh_token=refresh,
            expires_in=self.config.access_ttl_s,
            scope=_join(scopes),
            id_token=id_token,
        )

    async def exchange_code(
        self, *, client_id: str, code: str, code_verifier: str, redirect_uri: str
    ) -> TokenResponse:
        invalid = TokenEndpointError("invalid_grant", "The authorization code is invalid.")
        if not creds.looks_like(code, "code"):
            raise invalid
        now = self.clock()
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.scalars(
                        select(OAuthCode)
                        .where(OAuthCode.code_hash == creds.digest(code))
                        .with_for_update()
                    )
                ).first()
                if row is None:
                    raise invalid
                if row.used_at is not None:
                    # RFC 6749 §4.1.2: a replayed code revokes the tokens it produced.
                    await session.execute(
                        update(OAuthToken)
                        .where(OAuthToken.code_id == row.id, OAuthToken.revoked_at.is_(None))
                        .values(revoked_at=now)
                    )
                    issued = None
                elif (
                    row.expires_at <= now
                    or not creds.same(row.client_id, client_id)
                    or not creds.same(row.redirect_uri, redirect_uri)
                    or not creds.verify_pkce(code_verifier, row.code_challenge)
                ):
                    row.used_at = now  # a failed attempt burns the code as well
                    issued = None
                else:
                    row.used_at = now
                    issued = await self._redeem(session, row)
            # Committed: a burnt code or a replay revocation survives the error below.
            if issued is None:
                raise invalid
            return issued

    async def _redeem(self, session: AsyncSession, row: OAuthCode) -> TokenResponse | None:
        if row.request_id is not None:
            await session.execute(
                update(OAuthAuthorizationRequest)
                .where(OAuthAuthorizationRequest.id == row.request_id)
                .values(redirect_to=None)
            )
        grant = await session.get(OAuthGrant, row.grant_id, with_for_update=True)
        if grant is None or grant.revoked_at is not None:
            return None
        live = set(_split(grant.scopes))
        scopes = tuple(s for s in _split(row.scopes) if s in live)
        if not scopes:
            return None
        return await self._issue(
            session,
            grant=grant,
            scopes=scopes,
            resource=row.resource,
            family_id=uuid.uuid4(),
            code_id=row.id,
            nonce=row.nonce,
            auth_time=row.auth_time,
        )

    async def refresh(
        self, *, client_id: str, refresh_token: str, scope: str | None
    ) -> TokenResponse:
        invalid = TokenEndpointError("invalid_grant", "The refresh token is invalid.")
        if not creds.looks_like(refresh_token, "refresh"):
            raise invalid
        now = self.clock()
        async with self.sessions() as session:
            async with session.begin():
                row = (
                    await session.scalars(
                        select(OAuthToken)
                        .where(
                            OAuthToken.token_hash == creds.digest(refresh_token),
                            OAuthToken.kind == "refresh",
                        )
                        .with_for_update()
                    )
                ).first()
                if row is None or not creds.same(row.client_id, client_id):
                    raise invalid
                if row.rotated_at is not None and row.revoked_at is None:
                    # Reuse of a rotated refresh token: someone holds a copy. Kill the family.
                    await self._revoke_family(session, row.family_id, now)
                    reused = True
                else:
                    reused = False
                if not reused:
                    if row.revoked_at is not None or row.expires_at <= now:
                        raise invalid
                    grant = await session.get(OAuthGrant, row.grant_id, with_for_update=True)
                    if grant is None or grant.revoked_at is not None:
                        raise invalid
                    current = _split(row.scopes)
                    if scope is not None:
                        try:
                            wanted = parse_scope(scope)
                        except InvalidScopeError:
                            raise TokenEndpointError(
                                "invalid_scope", "The requested scope is invalid."
                            ) from None
                        if not set(wanted) <= set(current):
                            raise TokenEndpointError(
                                "invalid_scope", "The scope exceeds the original grant."
                            )
                        current = wanted
                    live = set(_split(grant.scopes))
                    scopes = tuple(s for s in current if s in live)
                    if not scopes:
                        raise invalid
                    row.rotated_at = now
                    return await self._issue(
                        session,
                        grant=grant,
                        scopes=scopes,
                        resource=row.resource,
                        family_id=row.family_id,
                        code_id=row.code_id,
                    )
            raise invalid

    async def _revoke_family(
        self, session: AsyncSession, family_id: uuid.UUID, now: datetime
    ) -> None:
        await session.execute(
            update(OAuthToken)
            .where(OAuthToken.family_id == family_id, OAuthToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def revoke(self, *, client_id: str, token: str) -> None:
        """RFC 7009: always succeeds; only the client's own token is affected."""
        if not (creds.looks_like(token, "access") or creds.looks_like(token, "refresh")):
            return
        now = self.clock()
        async with self.sessions() as session, session.begin():
            row = (
                await session.scalars(
                    select(OAuthToken)
                    .where(OAuthToken.token_hash == creds.digest(token))
                    .with_for_update()
                )
            ).first()
            if row is None or not creds.same(row.client_id, client_id):
                return
            if row.kind == "refresh":
                await self._revoke_family(session, row.family_id, now)
            elif row.revoked_at is None:
                row.revoked_at = now

    # -- resource side ---------------------------------------------------------------

    async def authenticate(self, token: str) -> AppAccess | None:
        if not creds.looks_like(token, "access"):
            return None
        now = self.clock()
        async with self.sessions() as session:
            found = (
                await session.execute(
                    select(OAuthToken, OAuthGrant)
                    .join(OAuthGrant, OAuthGrant.id == OAuthToken.grant_id)
                    .where(OAuthToken.token_hash == creds.digest(token))
                )
            ).first()
        if found is None:
            return None
        row, grant = found
        if (
            row.kind != "access"
            or not creds.same(row.token_hash, creds.digest(token))
            or row.revoked_at is not None
            or row.expires_at <= now
            or grant.revoked_at is not None
        ):
            return None
        live = set(_split(grant.scopes))
        return AppAccess(
            token_id=row.id,
            grant_id=grant.id,
            user_id=row.user_id,
            client_id=row.client_id,
            client_name=grant.client_name,
            scopes=frozenset(s for s in _split(row.scopes) if s in live),
            memory_categories=_categories(grant.memory_categories),
            monthly_limit_usd=grant.monthly_limit_usd,
            resource=row.resource,
            expires_at=row.expires_at,
        )

    async def userinfo(self, access: AppAccess) -> dict[str, Any]:
        """OIDC claims plus ``azp`` (client) and, for a resource-bound token, ``aud`` (M51-R1).

        A plugin server asks here who a token belongs to; it must check that ``aud`` is its
        own URL (and may check ``azp``), since Mia's tokens are opaque.
        """
        async with self.sessions() as session:
            user = await session.get(User, access.user_id)
        if user is None:
            raise app_error("invalid_token")
        body = self.claims(user, client_id=access.client_id, scopes=access.scopes)
        body["azp"] = access.client_id
        if access.resource is not None:
            body["aud"] = access.resource
        return body

    # -- tokens for ``server.auth: mia_token`` plugins (M51-R1) ----------------------

    async def plugin_grant(self, user_id: uuid.UUID, client_id: str) -> GrantView | None:
        """The user's live grant of a plugin's own client (its CIMD URL), if any."""
        async with self.sessions() as session:
            row = await self._active_grant(session, user_id, client_id)
        return _grant_view(row) if row is not None else None

    async def mint_plugin_token(
        self,
        user_id: uuid.UUID,
        *,
        client_id: str,
        resource: str,
        scopes: Sequence[str] = ("openid",),
    ) -> MintedToken | None:
        """A short-lived access token (no refresh) from the user's grant of ``client_id``.

        Bound to ``resource`` (the plugin server URL), scopes = ``scopes`` ∩ the live grant.
        None when the user has no active grant of that client or nothing intersects. The
        token row belongs to the grant: revoking the grant revokes it, and
        :meth:`authenticate` re-checks the grant on every use anyway.
        """
        now = self.clock()
        ttl = min(self.config.plugin_token_ttl_s, self.config.access_ttl_s, PLUGIN_TOKEN_MAX_TTL_S)
        async with self.sessions() as session, session.begin():
            grant = await self._active_grant(session, user_id, client_id)
            if grant is None:
                return None
            live = set(_split(grant.scopes))
            granted = tuple(dict.fromkeys(s for s in scopes if s in live))
            if not granted:
                return None
            access = creds.new_secret("access")
            expires_at = now + timedelta(seconds=ttl)
            session.add(
                OAuthToken(
                    id=uuid.uuid4(),
                    token_hash=creds.digest(access),
                    kind="access",
                    grant_id=grant.id,
                    family_id=uuid.uuid4(),
                    code_id=None,
                    user_id=grant.user_id,
                    client_id=grant.client_id,
                    scopes=_join(granted),
                    resource=resource,
                    created_at=now,
                    expires_at=expires_at,
                )
            )
        return MintedToken(
            access_token=access, scopes=granted, resource=resource, expires_at=expires_at
        )

    # -- connected apps (first-party) ------------------------------------------------

    async def list_grants(self, user_id: uuid.UUID) -> list[GrantView]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(OAuthGrant)
                    .where(OAuthGrant.user_id == user_id, OAuthGrant.revoked_at.is_(None))
                    .order_by(OAuthGrant.created_at.desc())
                )
            ).all()
        return [_grant_view(row) for row in rows]

    async def get_grant(self, user_id: uuid.UUID, grant_id: uuid.UUID) -> GrantView:
        async with self.sessions() as session:
            row = await session.get(OAuthGrant, grant_id)
        if row is None or row.user_id != user_id or row.revoked_at is not None:
            raise app_error("app_grant_not_found")
        return _grant_view(row)

    async def update_grant(
        self,
        user_id: uuid.UUID,
        grant_id: uuid.UUID,
        *,
        monthly_limit_usd: Decimal | None = None,
        memory_categories: Sequence[str] | None = None,
        remove_scopes: Sequence[str] = (),
    ) -> GrantView:
        async with self.sessions() as session, session.begin():
            row = await session.get(OAuthGrant, grant_id, with_for_update=True)
            if row is None or row.user_id != user_id or row.revoked_at is not None:
                raise app_error("app_grant_not_found")
            if monthly_limit_usd is not None:
                row.monthly_limit_usd = _money(
                    min(max(monthly_limit_usd, Decimal(0)), self.config.max_monthly_limit_usd)
                )
            if memory_categories is not None:
                row.memory_categories = _join(
                    [
                        c
                        for c in CATEGORIES
                        if c in set(memory_categories) and c not in SENSITIVE_CATEGORIES
                    ]
                )
            if remove_scopes:
                row.scopes = _join([s for s in _split(row.scopes) if s not in set(remove_scopes)])
            row.updated_at = self.clock()
            return _grant_view(row)

    async def revoke_grant(self, user_id: uuid.UUID, grant_id: uuid.UUID) -> None:
        """«Отключить приложение»: the grant and every token of it stop working at once."""
        now = self.clock()
        async with self.sessions() as session, session.begin():
            row = await session.get(OAuthGrant, grant_id, with_for_update=True)
            if row is None or row.user_id != user_id:
                raise app_error("app_grant_not_found")
            if row.revoked_at is not None:
                return  # repeating a disconnect is a no-op
            row.revoked_at = now
            await session.execute(
                update(OAuthToken)
                .where(OAuthToken.grant_id == grant_id, OAuthToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
