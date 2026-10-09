"""M17 DoD through the run engine (PostgreSQL run store + feed + router + memory tools).

- «Запомни, что Яна — моя жена» → suggested fact → confirm → ``people.resolve("Яна")``
  returns ``person:<id>``;
- no phone number reaches the provider (checked on every assembled model request);
- incognito runs write nothing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.core.ports.model_gateway import ModelRequest
from app.modules.conversations.feed import FeedService
from app.modules.memory.domain import PersonInput
from app.modules.memory.models import MemoryAccessLog, MemoryFact
from app.modules.runs import EngineConfig, NewRun, RunEngine, RunNotifier, RunService, SqlRunStore
from app.modules.tools.router import McpToolRouter
from sqlalchemy import func, select

from tests.fakes import FakeModelGateway, text_step, tool_step
from tests.memory.conftest import FakeContactsChannel, MemoryWorld
from tests.runs.conftest import TEST_LIMITS, assert_well_formed, collect, requires_postgres, types
from tests.runs.gateways import answer, scripted

pytestmark = requires_postgres

PHONE_DIGITS = ("79161234567", "9161234567", "123-45-67", "375291112233", "111-22-33")
# (no short fragments: random UUIDs in the stream could contain them)


def build_runs(mem: MemoryWorld, gateway: FakeModelGateway, router: McpToolRouter) -> RunService:
    engine = RunEngine(
        store=SqlRunStore(mem.factory),
        notifier=RunNotifier(),
        gateway=gateway,
        config=EngineConfig(limits=TEST_LIMITS),
        router=router,
        memory=mem.service,
        feed=FeedService(mem.factory),
    )
    return RunService(engine, keepalive_s=0.2)


async def run(
    service: RunService,
    user: uuid.UUID,
    text: str,
    *,
    incognito: bool = False,
    device_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    record, _ = await service.start(
        NewRun(
            user_id=user, profile="chat", input_text=text, incognito=incognito, device_id=device_id
        )
    )
    events = await collect(service, user, record.id)
    await service.wait_finished(record.id)
    assert_well_formed(events)
    assert events[-1]["type"] == "run.completed", events[-1]
    return events


def completed(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["payload"] for e in events if e["type"] == "tool.call.completed"]


def tool_messages(request: ModelRequest) -> list[dict[str, Any]]:
    return [json.loads(m.content) for m in request.messages if m.role == "tool"]


def everything_sent(gateway: FakeModelGateway) -> str:
    """Every byte the provider received: messages, tool calls and tool specs."""
    parts: list[str] = []
    for request in gateway.requests:
        for message in request.messages:
            parts.append(message.content)
            for call in message.tool_calls:
                parts.append(json.dumps(dict(call.arguments), ensure_ascii=False))
        for spec in request.tools:
            parts.append(json.dumps([spec.name, spec.description, dict(spec.input_schema)]))
    return "\n".join(parts)


async def test_remember_yana_confirm_then_resolve(mem: MemoryWorld) -> None:
    router, tools = mem.router()
    remember = scripted(
        tool_step("call_r1", "memory.remember", {"text": "Яна — моя жена", "category": "home"}),
        text_step(answer("Предлагаю запомнить, подтвердите.")),
    )
    service = build_runs(mem, remember, router)
    tools.bind_publisher(service.publish)

    events = await run(service, mem.user, "Запомни, что Яна — моя жена")

    assert "memory.remember" in {spec.name for spec in remember.requests[0].tools}
    assert types(events) == [
        "run.started",
        "tool.call.started",
        "memory.suggested",
        "tool.call.completed",
        "response.final",
        "run.completed",
    ]
    suggested = next(e["payload"] for e in events if e["type"] == "memory.suggested")
    assert suggested["text"] == "Яна — моя жена"
    assert suggested["category"] == "home"
    assert completed(events)[0]["status"] == "succeeded"
    assert completed(events)[0]["structured_content"]["status"] == "suggested"
    pending = await mem.service.list_facts(user_id=str(mem.user), status="suggested")
    assert [str(f.id) for f in pending.items] == [suggested["fact_id"]]
    assert await mem.service.resolve_person(user_id=str(mem.user), name="Яна") == []

    # One tap in the app: PATCH status=active.
    fact = await mem.service.update_fact(
        user_id=str(mem.user), fact_id=suggested["fact_id"], status="active"
    )
    assert fact.person_id is not None
    ref = f"person:{fact.person_id}"

    resolve = scripted(
        tool_step("call_p1", "people.resolve", {"name": "Яна"}),
        text_step(answer("Нашла Яну.")),
    )
    service = build_runs(mem, resolve, router)
    tools.bind_publisher(service.publish)
    events = await run(service, mem.user, "Напиши Яне, что надо купить молоко")

    framed = tool_messages(resolve.requests[1])[0]["content"]
    assert framed.startswith('<tool_result tool="people.resolve" trust="untrusted">')
    assert f'"ref": "{ref}", "name": "Яна", "source": "memory", "relation": "жена"' in framed
    assert completed(events)[0]["structured_content"]["candidates"][0]["ref"] == ref
    # The assembled context already names her by reference.
    system = resolve.requests[0].messages[0].content
    assert f"{ref}: Яна (жена)" in system


async def test_no_phone_number_reaches_the_provider(mem: MemoryWorld) -> None:
    user = str(mem.user)
    yana, _ = await mem.service.create_person(
        user_id=user,
        data=PersonInput("Яна", "жена", identifiers={"phone": "+79161234567"}),
    )
    await mem.service.create_fact(
        user_id=user, text="Телефон Яны +7 916 123-45-67", category="home"
    )
    channel = FakeContactsChannel(
        contacts=[{"display_name": "Олег Петров", "phones": ["+375 29 111-22-33"]}]
    )
    router, tools = mem.router(channel)
    gateway = scripted(
        tool_step("call_1", "people.resolve", {"name": "Яна"}),
        tool_step("call_2", "people.resolve", {"name": "Олег"}),
        tool_step(
            "call_3",
            "memory.remember",
            {
                "text": "Олег — мой коллега",
                "category": "work",
                "person": {
                    "name": "Олег Петров",
                    "relation": "коллега",
                    "contact_ref": "contact:1",
                },
            },
        ),
        text_step(answer("Готово.")),
    )
    service = build_runs(mem, gateway, router)
    tools.bind_publisher(service.publish)
    events = await run(
        service, mem.user, "Позвони Яне, потом Олегу, телефон Яны", device_id=uuid.uuid4()
    )

    sent = everything_sent(gateway)
    for digits in PHONE_DIGITS:
        assert digits not in sent, digits
    # The context did carry the people and facts — minimized.
    system = gateway.requests[0].messages[0].content
    assert f"person:{yana.id}: Яна (жена)" in system
    assert "Телефон Яны [номер скрыт]" in system
    # Memory first, then the phone's contacts as references only.
    results = completed(events)
    assert results[0]["structured_content"]["candidates"][0]["ref"] == f"person:{yana.id}"
    oleg = results[1]["structured_content"]["candidates"]
    assert oleg == [{"ref": "contact:1", "name": "Олег Петров", "source": "device_contacts"}]
    assert channel.calls[0].tool == "device.contacts.search"
    assert channel.calls[0].arguments == {"query": "Олег"}
    stream = json.dumps(events, ensure_ascii=False)
    for digits in PHONE_DIGITS:
        assert digits not in stream, digits

    # Nothing from the contacts was stored until the user confirms.
    assert [p.display_name for p in await mem.service.list_people(user_id=user)] == ["Яна"]
    suggested = await mem.service.list_facts(user_id=user, status="suggested")
    fact = await mem.service.update_fact(
        user_id=user, fact_id=str(suggested.items[0].id), status="active"
    )
    assert fact.person_id is not None
    ids = await mem.service.person_identifiers(user_id=user, person_id=str(fact.person_id))
    assert ids == {"phone": "+375291112233"}


async def test_incognito_writes_nothing(mem: MemoryWorld) -> None:
    user = str(mem.user)
    await mem.service.create_fact(user_id=user, text="Пьёт кофе без сахара", category="interests")
    await mem.service.create_person(user_id=user, data=PersonInput("Яна", "жена"))
    async with mem.factory() as session:
        log_before = await session.scalar(select(func.count()).select_from(MemoryAccessLog))
    router, tools = mem.router()
    gateway = scripted(
        tool_step("call_1", "memory.remember", {"text": "Любит чай", "category": "interests"}),
        tool_step("call_2", "people.resolve", {"name": "Яна"}),
        text_step(answer("Ок.")),
    )
    service = build_runs(mem, gateway, router)
    tools.bind_publisher(service.publish)
    events = await run(service, mem.user, "Запомни, что я люблю чай, и найди Яну", incognito=True)

    assert "memory.suggested" not in types(events)
    first, second = completed(events)
    assert first["status"] == "failed"
    assert first["error"]["code"] == "memory_incognito"
    assert second["status"] == "succeeded"
    # Nothing from memory went into the incognito context, nothing was written.
    assert "кофе" not in gateway.requests[0].messages[0].content
    async with mem.factory() as session:
        facts = await session.scalar(select(func.count()).select_from(MemoryFact))
        used = await session.scalar(
            select(func.count()).select_from(MemoryFact).where(MemoryFact.last_used_at.is_not(None))
        )
        log_after = await session.scalar(select(func.count()).select_from(MemoryAccessLog))
    assert facts == 1
    assert used == 0
    people = await mem.service.list_people(user_id=user)
    assert people[0].last_used_at is None
    assert log_after == (log_before or 0) + 1  # only the resolve read is audited


async def test_autosave_category_is_stored_without_a_suggestion(mem: MemoryWorld) -> None:
    await mem.service.update_settings(user_id=str(mem.user), autosave_categories=["interests"])
    router, tools = mem.router()
    gateway = scripted(
        tool_step("call_1", "memory.remember", {"text": "Любит джаз", "category": "interests"}),
        text_step(answer("Запомнила.")),
    )
    service = build_runs(mem, gateway, router)
    tools.bind_publisher(service.publish)
    events = await run(service, mem.user, "Запомни, что я люблю джаз")
    assert "memory.suggested" not in types(events)
    assert completed(events)[0]["structured_content"]["status"] == "active"
    active = await mem.service.list_facts(user_id=str(mem.user), status="active")
    assert [f.text for f in active.items] == ["Любит джаз"]
