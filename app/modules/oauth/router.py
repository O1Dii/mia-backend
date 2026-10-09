"""HTTP surface of «Войти через Mia».

Authorization server (public, RFC 8414 / 6749 / 7636 / 7009 / 9207 / OIDC):
``/.well-known/oauth-authorization-server``, ``/.well-known/openid-configuration``,
``/oauth/authorize``, ``/oauth/token``, ``/oauth/revoke``, ``/oauth/userinfo``,
``/oauth/jwks.json``; protected resource metadata (RFC 9728) for the API and ``/mcp``.

Consent and «Подключённые приложения» (first-party principal: web cookie or bearer):
``/v1/oauth/requests/{handle}`` (+ ``/decision``) and ``/v1/apps*``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Header, Path, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import FormData

from app.modules.memory.domain import CATEGORIES
from app.modules.memory.service import MemoryService
from app.modules.oauth.errors import (
    APP_ERROR_CODES,
    AppApiError,
    AuthorizeError,
    TokenEndpointError,
)
from app.modules.oauth.principal import AppAuthenticator
from app.modules.oauth.scopes import FIXED_SCOPES, MEMORY_READ_PREFIX, NOT_ENFORCED_YET
from app.modules.oauth.service import (
    AuthorizationService,
    AuthorizeParams,
    Decision,
    GrantView,
    with_query,
)
from app.modules.oauth.usage import UsageLedger, month_start

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
Handle = Annotated[str, Path(min_length=8, max_length=80)]
Category = Literal["profile", "work", "home", "health", "finance", "interests", "other"]
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
AUTHORIZE_PARAMS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
    "resource",
    "nonce",
)


class FirstPartyPrincipal(Protocol):
    @property
    def user_id(self) -> str: ...


class FirstPartyAuthenticator(Protocol):
    async def authenticate(self, request: Request) -> FirstPartyPrincipal: ...


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]
    scopes: list[Annotated[str, Field(min_length=1, max_length=120)]] | None = Field(
        default=None, max_length=32
    )
    memory_categories: list[Category] | None = None
    monthly_limit_usd: Decimal | None = Field(default=None, ge=0, le=100000, decimal_places=2)


class GrantPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_limit_usd: Decimal | None = Field(default=None, ge=0, le=100000, decimal_places=2)
    memory_categories: list[Category] | None = None
    remove_scopes: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=32
    )


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppApiError):
        raise exc
    retryable = APP_ERROR_CODES.get(exc.code, (0, False, ""))[1]
    return JSONResponse(
        status_code=exc.status,
        headers=exc.headers,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": str(getattr(request.state, "request_id", "request")),
                "retryable": retryable,
            }
        },
    )


def _token_error(error: TokenEndpointError) -> JSONResponse:
    headers = dict(NO_STORE)
    if error.status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="mia"'
    return JSONResponse(
        status_code=error.status,
        headers=headers,
        content={"error": error.error, "error_description": error.description},
    )


def _single(form: FormData, name: str) -> str | None:
    values = form.getlist(name)
    if len(values) > 1:
        raise TokenEndpointError("invalid_request", f"Parameter {name} is repeated.")
    if not values:
        return None
    value = values[0]
    if not isinstance(value, str):
        raise TokenEndpointError("invalid_request", f"Parameter {name} is invalid.")
    return value


async def _form(request: Request) -> FormData:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise TokenEndpointError("invalid_request", "Use application/x-www-form-urlencoded.")
    if int(request.headers.get("content-length") or 0) > 16 * 1024:
        raise TokenEndpointError("invalid_request", "The request body is too large.")
    return await request.form()


def _grant_body(view: GrantView, spent: Decimal | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(view.id),
        "client_id": view.client_id,
        "client_name": view.client_name,
        "client_uri": view.client_uri,
        "logo_uri": view.logo_uri,
        "scopes": list(view.scopes),
        "memory_categories": list(view.memory_categories),
        "monthly_limit_usd": str(view.monthly_limit_usd),
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
    }
    if spent is not None:
        body["month_spend_usd"] = str(spent.quantize(Decimal("0.000001")))
    return body


def authorization_metadata(service: AuthorizationService) -> dict[str, Any]:
    issuer = service.config.issuer
    base = issuer.rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "revocation_endpoint": base + "/oauth/revoke",
        "userinfo_endpoint": base + "/oauth/userinfo",
        "jwks_uri": base + "/oauth/jwks.json",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "scopes_supported": sorted(FIXED_SCOPES) + [MEMORY_READ_PREFIX + c for c in CATEGORIES],
        "subject_types_supported": ["pairwise"],
        "id_token_signing_alg_values_supported": ["ES256"],
        "claims_supported": [
            "sub",
            "name",
            "picture",
            "locale",
            "zoneinfo",
            "email",
            "email_verified",
        ],
    }


def build_oauth_router(
    *,
    service: AuthorizationService,
    apps: AppAuthenticator,
    first_party: FirstPartyAuthenticator,
    ledger: UsageLedger,
    memory: MemoryService | None = None,
) -> APIRouter:
    router = APIRouter(tags=["Apps"])
    config = service.config

    async def user_of(request: Request) -> uuid.UUID:
        principal = await first_party.authenticate(request)
        return uuid.UUID(principal.user_id)

    # -- metadata --------------------------------------------------------------

    @router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    @router.get("/.well-known/openid-configuration", include_in_schema=False)
    async def metadata() -> JSONResponse:
        return JSONResponse(authorization_metadata(service))

    def resource_metadata(resource: str) -> JSONResponse:
        return JSONResponse(
            {
                "resource": resource,
                "authorization_servers": [config.issuer],
                "bearer_methods_supported": ["header"],
                "scopes_supported": sorted(FIXED_SCOPES - {"openid", "profile", "email"}),
                "resource_name": "Mia",
            }
        )

    @router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    async def api_resource_metadata() -> JSONResponse:
        return resource_metadata(apps.api_resource)

    @router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    async def mcp_resource_metadata() -> JSONResponse:
        return resource_metadata(apps.mcp_resource)

    @router.get("/oauth/jwks.json", include_in_schema=False)
    async def jwks() -> JSONResponse:
        return JSONResponse(service.signer.jwks())

    # -- authorize / token / revoke -----------------------------------------------

    @router.get("/oauth/authorize", include_in_schema=False)
    async def authorize(request: Request) -> Response:
        query = request.query_params
        values: dict[str, str | None] = {}
        for name in AUTHORIZE_PARAMS:
            items = query.getlist(name)
            if len(items) > 1:
                return JSONResponse(
                    status_code=400,
                    headers=NO_STORE,
                    content={"error": "invalid_request", "error_description": f"{name} repeated"},
                )
            values[name] = items[0] if items else None
        params = AuthorizeParams(
            client_id=values["client_id"] or "",
            redirect_uri=values["redirect_uri"] or "",
            response_type=values["response_type"] or "",
            scope=values["scope"] or "",
            state=values["state"] or "",
            code_challenge=values["code_challenge"] or "",
            code_challenge_method=values["code_challenge_method"] or "",
            resource=values["resource"],
            nonce=values["nonce"],
        )
        try:
            target = await service.authorize(params)
        except AuthorizeError as error:
            if error.redirect_uri is None:
                return JSONResponse(
                    status_code=400,
                    headers=NO_STORE,
                    content={"error": error.error, "error_description": error.description},
                )
            back: dict[str, str] = {"error": error.error, "error_description": error.description}
            if error.state:
                back["state"] = error.state
            back["iss"] = config.issuer
            return RedirectResponse(with_query(error.redirect_uri, back), status_code=302)
        return RedirectResponse(target, status_code=302, headers=NO_STORE)

    @router.post("/oauth/token", include_in_schema=False)
    async def token(request: Request) -> JSONResponse:
        try:
            if request.headers.get("authorization"):
                raise TokenEndpointError(
                    "invalid_client", "Only public clients (no secret) are supported.", status=401
                )
            form = await _form(request)
            grant_type = _single(form, "grant_type")
            client_id = _single(form, "client_id")
            if not client_id:
                raise TokenEndpointError("invalid_client", "client_id is required.", status=401)
            if grant_type == "authorization_code":
                code = _single(form, "code")
                verifier = _single(form, "code_verifier")
                redirect_uri = _single(form, "redirect_uri")
                if not code or not verifier or not redirect_uri:
                    raise TokenEndpointError(
                        "invalid_request", "code, code_verifier and redirect_uri are required."
                    )
                issued = await service.exchange_code(
                    client_id=client_id,
                    code=code,
                    code_verifier=verifier,
                    redirect_uri=redirect_uri,
                )
            elif grant_type == "refresh_token":
                refresh_token = _single(form, "refresh_token")
                if not refresh_token:
                    raise TokenEndpointError("invalid_request", "refresh_token is required.")
                issued = await service.refresh(
                    client_id=client_id, refresh_token=refresh_token, scope=_single(form, "scope")
                )
            else:
                raise TokenEndpointError("unsupported_grant_type", "Unsupported grant_type.")
        except TokenEndpointError as error:
            return _token_error(error)
        return JSONResponse(issued.body(), headers=NO_STORE)

    @router.post("/oauth/revoke", include_in_schema=False)
    async def revoke(request: Request) -> Response:
        try:
            form = await _form(request)
            token_value = _single(form, "token")
            client_id = _single(form, "client_id")
            if not token_value:
                raise TokenEndpointError("invalid_request", "token is required.")
            if not client_id:
                raise TokenEndpointError("invalid_client", "client_id is required.", status=401)
        except TokenEndpointError as error:
            return _token_error(error)
        await service.revoke(client_id=client_id, token=token_value)
        return Response(status_code=200, headers=NO_STORE)

    @router.get("/oauth/userinfo", include_in_schema=False)
    @router.post("/oauth/userinfo", include_in_schema=False)
    async def userinfo(request: Request) -> JSONResponse:
        access = await apps.authenticate(request, required=("openid",), any_audience=True)
        return JSONResponse(await service.userinfo(access), headers=NO_STORE)

    # -- consent (first-party) ----------------------------------------------------

    @router.get("/v1/oauth/requests/{handle}")
    async def get_request(request: Request, handle: Handle) -> JSONResponse:
        view = await service.view_request(handle, await user_of(request))
        return JSONResponse(
            {
                "client": {
                    "client_id": view.client_id,
                    "name": view.client_name,
                    "client_uri": view.client_uri,
                    "logo_uri": view.logo_uri,
                    "redirect_host": view.redirect_host,
                },
                "scopes": [
                    {
                        "scope": s.scope,
                        "title": s.title,
                        "description": s.description,
                        "sensitive": s.sensitive,
                        "available": s.scope not in NOT_ENFORCED_YET,
                    }
                    for s in view.scopes
                ],
                "memory_categories": list(view.memory_categories),
                "monthly_limit_usd": str(view.monthly_limit_usd),
                "max_monthly_limit_usd": str(view.max_monthly_limit_usd),
                "existing_grant": view.existing_grant,
                "expires_at": view.expires_at.isoformat(),
            },
            headers=NO_STORE,
        )

    @router.post("/v1/oauth/requests/{handle}/decision")
    async def decide(
        request: Request, handle: Handle, body: DecisionBody, idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        user = await user_of(request)
        redirect_to = await service.decide(
            handle,
            user,
            Decision(
                approve=body.decision == "approve",
                scopes=tuple(body.scopes) if body.scopes is not None else None,
                memory_categories=(
                    tuple(body.memory_categories) if body.memory_categories is not None else None
                ),
                monthly_limit_usd=body.monthly_limit_usd,
            ),
            idempotency_key=idempotency_key,
        )
        return JSONResponse({"redirect_to": redirect_to}, headers=NO_STORE)

    # -- connected apps (first-party) ---------------------------------------------

    @router.get("/v1/apps")
    async def list_apps(request: Request) -> JSONResponse:
        user = await user_of(request)
        items = []
        for view in await service.list_grants(user):
            items.append(_grant_body(view, await ledger.month_spend(view.id)))
        return JSONResponse({"items": items})

    @router.get("/v1/apps/{grant_id}")
    async def get_app(request: Request, grant_id: uuid.UUID) -> JSONResponse:
        view = await service.get_grant(await user_of(request), grant_id)
        return JSONResponse(_grant_body(view, await ledger.month_spend(view.id)))

    @router.patch("/v1/apps/{grant_id}")
    async def patch_app(
        request: Request, grant_id: uuid.UUID, body: GrantPatch, idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        view = await service.update_grant(
            await user_of(request),
            grant_id,
            monthly_limit_usd=body.monthly_limit_usd,
            memory_categories=body.memory_categories,
            remove_scopes=body.remove_scopes,
        )
        return JSONResponse(_grant_body(view, await ledger.month_spend(view.id)))

    @router.delete("/v1/apps/{grant_id}")
    async def delete_app(
        request: Request, grant_id: uuid.UUID, idempotency_key: IdempotencyKey
    ) -> Response:
        await service.revoke_grant(await user_of(request), grant_id)
        return Response(status_code=204)

    @router.get("/v1/apps/{grant_id}/usage")
    async def app_usage(
        request: Request, grant_id: uuid.UUID, limit: int = Query(default=100, ge=1, le=500)
    ) -> JSONResponse:
        view = await service.get_grant(await user_of(request), grant_id)
        entries = await ledger.entries(view.id, limit=limit)
        return JSONResponse(
            {
                "month_start": month_start(ledger.clock()).isoformat(),
                "month_spend_usd": str(await ledger.month_spend(view.id)),
                "monthly_limit_usd": str(view.monthly_limit_usd),
                "items": [
                    {
                        "at": e.at.isoformat(),
                        "surface": e.surface,
                        "run_id": str(e.run_id) if e.run_id else None,
                        "input_tokens": e.input_tokens,
                        "output_tokens": e.output_tokens,
                        "cost_usd": str(e.cost_usd),
                        "estimated": e.estimated,
                    }
                    for e in entries
                ],
            }
        )

    @router.get("/v1/apps/{grant_id}/access-log")
    async def app_access_log(
        request: Request, grant_id: uuid.UUID, limit: int = Query(default=100, ge=1, le=500)
    ) -> JSONResponse:
        user = await user_of(request)
        view = await service.get_grant(user, grant_id)
        items: list[dict[str, Any]] = []
        if memory is not None:
            for entry in await memory.access_log(user_id=str(user), limit=500):
                if entry.app_client_id == view.client_id:
                    items.append(entry.contract())
                if len(items) >= limit:
                    break
        return JSONResponse({"items": items})

    return router
