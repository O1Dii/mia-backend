"""``/v1/memory*``: «Что Mia знает обо мне» (contracts/mvp/v1/openapi.yaml, ADR-0012).

Owner-only: a record of another user answers 404 like a missing one. Android
(bearer) and web (``mia_session`` cookie) share one principal. Every mutation
takes ``Idempotency-Key``; creates and imports replay their first result.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Path, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from app.core.ports.memory_provider import MemoryCategory
from app.modules.auth.principal import RequestAuthenticator
from app.modules.memory.domain import HistoryRetention, PersonInput, clean_identifiers
from app.modules.memory.errors import MemoryUnavailableError
from app.modules.memory.service import MemoryService
from app.modules.memory.transfer import ImportFormat, export_json, export_zip, parse_import

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
Category = Literal["profile", "work", "home", "health", "finance", "interests", "other"]
PREFERENCE_KEY = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,3}$"


class FactCreate(BaseModel):
    """``MemorySuggestion``: a user-authored fact (active at once)."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    category: Category


class FactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    category: Category | None = None
    status: Literal["active", "rejected"] | None = None
    scope: Literal["user", "automation"] | None = None


class Identifiers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    telegram_user_id: int | None = Field(default=None, ge=1)
    telegram_username: str | None = Field(default=None, pattern=r"^@?[A-Za-z0-9_]{5,32}$")
    phone: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{6,14}$")
    email: str | None = Field(default=None, max_length=254)


class PersonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=120)
    aliases: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list, max_length=20
    )
    relation: str | None = Field(default=None, min_length=1, max_length=60)
    identifiers: Identifiers | None = None
    trusted: bool = False


class PersonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    aliases: list[Annotated[str, Field(min_length=1, max_length=60)]] | None = Field(
        default=None, max_length=20
    )
    relation: str | None = Field(default=None, max_length=60)
    identifiers: Identifiers | None = None
    trusted: bool | None = None
    status: Literal["active", "rejected"] | None = None


class PreferencePut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | int | float | bool


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["markdown", "text", "json"]
    source: Literal["chatgpt", "claude", "gemini", "mia", "other"] = "other"
    content: str = Field(min_length=1)


class SettingsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    autosave_categories: list[Category] | None = None
    history_retention: Literal["30d", "1y", "forever", "none"] | None = None


def _identifiers(value: Identifiers | None) -> dict[str, str | int]:
    return clean_identifiers(value.model_dump(exclude_none=True)) if value else {}


def build_memory_router(
    *,
    service: MemoryService,
    authenticator: RequestAuthenticator,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/v1/memory", tags=["Memory"])

    async def user(request: Request) -> str:
        return (await authenticator.authenticate(request)).user_id

    async def guard(operation: Any) -> Any:
        try:
            return await operation
        except SQLAlchemyError as exc:
            raise MemoryUnavailableError() from exc

    # -- facts -------------------------------------------------------------------

    @router.get("/facts")
    async def list_facts(
        request: Request,
        status: Annotated[Literal["suggested", "active", "rejected"] | None, Query()] = None,
        category: Annotated[Category | None, Query()] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        page = await guard(
            service.list_facts(
                user_id=await user(request),
                status=status,
                category=category,
                cursor=cursor,
                limit=limit,
            )
        )
        return JSONResponse(
            {"items": [f.contract() for f in page.items], "next_cursor": page.next_cursor}
        )

    @router.post("/facts", status_code=201)
    async def create_fact(
        request: Request, body: Annotated[FactCreate, Body()], idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        fact, created = await guard(
            service.create_fact(
                user_id=await user(request),
                text=body.text,
                category=body.category,
                idempotency_key=idempotency_key,
            )
        )
        return JSONResponse(fact.contract(), status_code=201 if created else 200)

    @router.patch("/facts/{fact_id}")
    async def update_fact(
        fact_id: str,
        request: Request,
        body: Annotated[FactPatch, Body()],
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        fact = await guard(
            service.update_fact(
                user_id=await user(request),
                fact_id=fact_id,
                text=body.text,
                category=body.category,
                status=body.status,
                scope=body.scope,
            )
        )
        return JSONResponse(fact.contract())

    @router.delete("/facts/{fact_id}", status_code=204)
    async def delete_fact(
        fact_id: str, request: Request, idempotency_key: IdempotencyKey
    ) -> Response:
        await guard(service.delete_fact(user_id=await user(request), fact_id=fact_id))
        return Response(status_code=204)

    # -- people ------------------------------------------------------------------

    @router.get("/people")
    async def list_people(
        request: Request,
        status: Annotated[Literal["suggested", "active", "rejected"] | None, Query()] = None,
    ) -> JSONResponse:
        people = await guard(service.list_people(user_id=await user(request), status=status))
        return JSONResponse({"items": [p.contract() for p in people]})

    @router.post("/people", status_code=201)
    async def create_person(
        request: Request, body: Annotated[PersonCreate, Body()], idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        person, created = await guard(
            service.create_person(
                user_id=await user(request),
                data=PersonInput(
                    display_name=body.display_name,
                    relation=body.relation,
                    aliases=tuple(body.aliases),
                    identifiers=_identifiers(body.identifiers),
                ),
                trusted=body.trusted,
                idempotency_key=idempotency_key,
            )
        )
        return JSONResponse(person.contract(), status_code=201 if created else 200)

    @router.patch("/people/{person_id}")
    async def update_person(
        person_id: str,
        request: Request,
        body: Annotated[PersonPatch, Body()],
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        changes: dict[str, Any] = body.model_dump(exclude_unset=True)
        if "identifiers" in changes:
            changes["identifiers"] = _identifiers(body.identifiers)
        person = await guard(
            service.update_person(user_id=await user(request), person_id=person_id, changes=changes)
        )
        return JSONResponse(person.contract())

    @router.delete("/people/{person_id}", status_code=204)
    async def delete_person(
        person_id: str, request: Request, idempotency_key: IdempotencyKey
    ) -> Response:
        await guard(service.delete_person(user_id=await user(request), person_id=person_id))
        return Response(status_code=204)

    # -- preferences -------------------------------------------------------------

    @router.get("/preferences")
    async def list_preferences(request: Request) -> JSONResponse:
        items = await guard(service.list_preferences(user_id=await user(request)))
        return JSONResponse({"items": [p.contract() for p in items]})

    @router.put("/preferences/{key}")
    async def put_preference(
        key: Annotated[str, Path(pattern=PREFERENCE_KEY, max_length=128)],
        request: Request,
        body: Annotated[PreferencePut, Body()],
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        preference = await guard(
            service.put_preference(user_id=await user(request), key=key, value=body.value)
        )
        return JSONResponse(preference.contract())

    # -- context / export / import ----------------------------------------------

    @router.get("/context")
    async def get_context(
        request: Request,
        query: Annotated[str, Query(min_length=1, max_length=2000)],
    ) -> JSONResponse:
        text = await guard(service.render_context(user_id=await user(request), query=query))
        return JSONResponse({"context": text})

    @router.get("/export")
    async def export_memory(
        request: Request, format: Annotated[Literal["zip", "json"], Query()] = "zip"
    ) -> Response:
        snapshot = await guard(service.snapshot(user_id=await user(request)))
        now = clock()
        if format == "json":
            return JSONResponse(export_json(snapshot, exported_at=now))
        stamp = now.strftime("%Y%m%d")
        return Response(
            content=export_zip(snapshot, exported_at=now),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="mia-memory-{stamp}.zip"'},
        )

    @router.post("/import", status_code=201)
    async def import_memory(
        request: Request, body: Annotated[ImportBody, Body()], idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        fmt: ImportFormat = body.format
        items = parse_import(body.content, fmt)
        result = await guard(
            service.import_items(
                user_id=await user(request),
                items=items,
                idempotency_key=idempotency_key,
                request_body=body.model_dump(),
            )
        )
        return JSONResponse(result.contract(), status_code=201)

    # -- settings / access log / erase ------------------------------------------

    @router.get("/settings")
    async def get_settings(request: Request) -> JSONResponse:
        settings = await guard(service.settings(user_id=await user(request)))
        return JSONResponse(settings.contract())

    @router.put("/settings")
    async def put_settings(
        request: Request, body: Annotated[SettingsPut, Body()], idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        categories: list[MemoryCategory] | None = body.autosave_categories
        retention: HistoryRetention | None = body.history_retention
        settings = await guard(
            service.update_settings(
                user_id=await user(request),
                autosave_categories=categories,
                history_retention=retention,
            )
        )
        return JSONResponse(settings.contract())

    @router.get("/access-log")
    async def access_log(
        request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50
    ) -> JSONResponse:
        entries = await guard(service.access_log(user_id=await user(request), limit=limit))
        return JSONResponse({"items": [entry.contract() for entry in entries]})

    @router.delete("", status_code=204)
    async def forget_everything(request: Request, idempotency_key: IdempotencyKey) -> Response:
        await guard(service.forget_all(user_id=await user(request)))
        return Response(status_code=204)

    return router
