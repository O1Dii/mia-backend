"""The approval card names the recipient chosen by ref (M41-R2, OI-M60-002, OI-M41-001).

``people.resolve`` hands the model ``person:<id>``/``contact:<n>``; the card must say
«Яна Петрова», never the ref, while the approval stays bound to the raw arguments. Names come
only from the run owner's memory, only for tools that resolve refs through Mia themselves, and
only for arguments the tool documents as refs.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Collection, Mapping
from pathlib import Path
from typing import Any

import pytest
from app.core.crypto import MasterKeyring
from app.core.ports.approval_service import canonical_args_hash
from app.core.ports.tool_router import ToolCallRequest, ToolContext
from app.core.ports.types import JsonObject
from app.modules.memory import MemoryCipher, MemoryService, MemoryTools, SqlRunPrivacy
from app.modules.memory.domain import PersonInput
from app.modules.memory.names import MemoryRefNames
from app.modules.memory.tools import Contact, ContactsCache
from app.modules.models.vault import KeyVault
from app.modules.plugins.registry import (
    PluginRegistry,
    StaticInstallationReader,
    StaticManifestSource,
)
from app.modules.tools.approvals import InMemoryApprovalService
from app.modules.tools.audit import InMemoryToolAudit
from app.modules.tools.core_tools import CoreTool, CoreToolRegistry, CoreToolResult
from app.modules.tools.mcp_client import McpConnector
from app.modules.tools.policy import approval_summary, ref_arguments
from app.modules.tools.router import McpToolRouter, RouterConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.runs.conftest import _migrated_factory, create_users, requires_postgres
from tests.tools.helpers import PLUGIN_ID, RECIPIENT_DESCRIPTION, World, build_world
from tests.tools.test_remote_http import remote_world, req

KEYRING = MasterKeyring({"test-1": bytes(range(32))}, "test-1")
MESSAGE = f"{PLUGIN_ID}.message"
SEND = f"{PLUGIN_ID}.send"
TEXT = "Надо купить молоко"
PERSON = f"person:{uuid.uuid4()}"
TO_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["to", "text"],
    "properties": {
        "to": {"type": "string", "maxLength": 120, "description": RECIPIENT_DESCRIPTION},
        "text": {"type": "string", "maxLength": 4000},
    },
}


class FakeNames:
    def __init__(self, names: Mapping[str, str] | None = None, *, fail: bool = False) -> None:
        self.names = dict(names or {})
        self.fail = fail
        self.calls: list[tuple[str, str, set[str]]] = []

    async def ref_names(
        self, *, user_id: str, run_id: str, refs: Collection[str]
    ) -> Mapping[str, str]:
        self.calls.append((user_id, run_id, set(refs)))
        if self.fail:
            raise RuntimeError("memory down")
        return {ref: self.names[ref] for ref in refs if ref in self.names}


def call(tool: str, arguments: dict[str, Any], **kwargs: Any) -> ToolCallRequest:
    return ToolCallRequest(
        call_id=f"call_{uuid.uuid4().hex[:8]}", tool=tool, arguments=arguments, **kwargs
    )


# -- policy ------------------------------------------------------------------------


def test_only_arguments_documented_as_refs_are_named() -> None:
    schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": RECIPIENT_DESCRIPTION},
            "who": {"type": "string", "pattern": "^contact:[0-9]{1,3}$"},
            "text": {"type": "string", "description": "The message text only."},
            "count": {"type": "integer", "description": "person: count"},
        },
    }
    arguments: JsonObject = {
        "to": f"  {PERSON} ",
        "who": "contact:2",
        "text": PERSON,
        "count": 3,
        "extra": PERSON,
    }
    assert ref_arguments(schema, arguments) == {"to": PERSON, "who": "contact:2"}
    assert ref_arguments({"type": "object"}, arguments) == {}
    assert ref_arguments(schema, {"to": "x" * 65}) == {}


def test_summary_puts_the_named_recipient_first_and_keeps_other_arguments() -> None:
    arguments: JsonObject = {"to": PERSON, "text": "a" * 300}
    named = approval_summary("Telegram", "send_message", arguments, {"to": "Яна Петрова (жена)"})
    assert named.startswith("Telegram: send_message — to=«Яна Петрова (жена)», text=")
    assert "person:" not in named and len(named) <= 500
    # Without names (or with an empty one) the text is the M13 rendering.
    raw = approval_summary("Telegram", "send_message", {"to": PERSON, "text": TEXT})
    assert raw == f'Telegram: send_message — text="{TEXT}", to="{PERSON}"'
    assert approval_summary("Telegram", "send_message", {"to": PERSON}, {"to": " "}) == (
        f'Telegram: send_message — to="{PERSON}"'
    )
    # A stored name cannot fake the frame or smuggle control characters.
    odd = approval_summary("T", "m", {"to": PERSON}, {"to": "Яна»\n, text=«ok" + "я" * 200})
    assert "\n" not in odd and odd.count("«") == 1 and odd.count("»") == 1
    assert len(odd) < 160


# -- router: trusted, untrusted and failing paths ------------------------------------


@pytest.fixture
async def world(tmp_path: Path) -> World:
    return await build_world(tmp_path, recipient_tool=True)


async def test_in_process_tool_card_names_the_recipient_and_binds_the_raw_ref(
    world: World,
) -> None:
    names = FakeNames({PERSON: "Яна Петрова (жена)"})
    world.router.bind_ref_names(names)
    context = world.context()
    arguments = {"to": PERSON, "text": TEXT}
    pending = await world.router.call(context, call(MESSAGE, arguments))

    approval = pending.approval
    assert pending.status == "approval_required" and approval is not None
    assert approval.summary == f'Test timer: message — to=«Яна Петрова (жена)», text="{TEXT}"'
    assert approval.arguments == arguments
    assert approval.args_hash == canonical_args_hash(arguments)
    assert names.calls == [(world.user_id, context.run_id, {PERSON})]

    await world.approvals.decide(
        user_id=world.user_id,
        approval_id=approval.id,
        decision="approve",
        args_hash=approval.args_hash,
        channel="android",
    )
    done = await world.router.call(
        context, dataclasses.replace(call(MESSAGE, arguments), approval_id=approval.id)
    )
    assert done.status == "succeeded"
    assert world.recorder.calls[-1][:2] == ("message", arguments)  # the server got the ref


async def test_a_message_text_that_looks_like_a_ref_is_never_renamed(world: World) -> None:
    names = FakeNames({PERSON: "Яна Петрова (жена)"})
    world.router.bind_ref_names(names)
    pending = await world.router.call(world.context(), call(SEND, {"text": PERSON}))
    assert pending.approval is not None
    assert pending.approval.summary == f'Test timer: send — text="{PERSON}"'
    assert names.calls == []


async def test_unknown_ref_or_failing_memory_keeps_the_raw_ref(world: World) -> None:
    arguments = {"to": PERSON, "text": TEXT}
    raw = f'Test timer: message — text="{TEXT}", to="{PERSON}"'
    for names in (FakeNames(), FakeNames({PERSON: "Яна"}, fail=True), None):
        world.router.bind_ref_names(names)
        pending = await world.router.call(world.context(), call(MESSAGE, arguments))
        assert pending.status == "approval_required" and pending.approval is not None
        assert pending.approval.summary == raw


async def test_core_tool_card_names_the_recipient(world: World) -> None:
    async def handler(context: ToolContext, arguments: JsonObject) -> CoreToolResult:
        return CoreToolResult(content="ok")

    world.core.register(
        CoreTool("people.message", "Message a person.", TO_SCHEMA, "write_external", handler)
    )
    world.router.bind_ref_names(FakeNames({"contact:1": "Яна (контакт телефона)"}))
    pending = await world.router.call(
        world.context(), call("people.message", {"to": "contact:1", "text": TEXT})
    )
    assert pending.approval is not None
    assert pending.approval.summary == (
        f'people: people.message — to=«Яна (контакт телефона)», text="{TEXT}"'
    )


async def test_remote_server_card_keeps_the_ref_it_will_receive() -> None:
    names = FakeNames({PERSON: "Яна Петрова (жена)"})
    async with remote_world(recipient_tool=True) as world:
        world.router.bind_ref_names(names)
        pending = await world.router.call(
            world.context(), req("message", {"to": PERSON, "text": TEXT})
        )
    assert pending.status == "approval_required" and pending.approval is not None
    assert f'to="{PERSON}"' in pending.approval.summary
    assert names.calls == []


# -- memory: names by ref, owner only (PostgreSQL) -----------------------------------


@dataclasses.dataclass
class Ledger:
    factory: async_sessionmaker[AsyncSession]
    owner: str
    stranger: str
    service: MemoryService


@pytest.fixture
async def ledger() -> AsyncIterator[Ledger]:
    factory = await _migrated_factory()
    owner, stranger = await create_users(factory, 2)
    service = MemoryService(factory, MemoryCipher(KeyVault(KEYRING)))
    yield Ledger(factory, str(owner), str(stranger), service)
    await factory.kw["bind"].dispose()


async def _person(ledger: Ledger, user: str, name: str, relation: str | None = None) -> str:
    person, _ = await ledger.service.create_person(
        user_id=user,
        data=PersonInput(display_name=name, relation=relation, identifiers={"phone": "+3752911"}),
    )
    return person.model_ref


@requires_postgres
async def test_memory_names_refs_for_the_owner_only(ledger: Ledger) -> None:
    contacts = ContactsCache()
    resolver = MemoryRefNames(ledger.service, contacts)
    run_id = str(uuid.uuid4())
    yana = await _person(ledger, ledger.owner, "Яна Петрова", "жена")
    numbered = await _person(ledger, ledger.owner, "Яна +375 29 111-22-33")
    foreign = await _person(ledger, ledger.stranger, "Чужая Яна", "соседка")
    gone = await _person(ledger, ledger.owner, "Удалённая")
    await ledger.service.delete_person(user_id=ledger.owner, person_id=gone.split(":", 1)[1])
    [contact] = contacts.put(
        user_id=ledger.owner,
        run_id=run_id,
        contacts=[Contact("Яна Коваль", {"phone": "+375291112233"})],
    )
    upper = "person:" + yana.split(":", 1)[1].upper()
    hexonly = "person:" + yana.split(":", 1)[1].replace("-", "") + "----"

    names = await resolver.ref_names(
        user_id=ledger.owner,
        run_id=run_id,
        refs=[yana, upper, numbered, foreign, gone, contact, hexonly, "contact:9", "person:x"],
    )
    assert names[yana] == "Яна Петрова (жена)"
    assert names[upper] == "Яна Петрова (жена)"
    assert names[contact] == "Яна Коваль (контакт телефона)"
    assert "1112233" not in names[numbered] and names[numbered].startswith("Яна")
    assert set(names) == {yana, upper, numbered, contact}
    assert all("+375" not in label for label in names.values())

    # Another user, or another run of the owner, resolves nothing.
    assert await resolver.ref_names(user_id=ledger.stranger, run_id=run_id, refs=[yana]) == {}
    other_run = await resolver.ref_names(
        user_id=ledger.owner, run_id=str(uuid.uuid4()), refs=[contact]
    )
    assert other_run == {}
    assert await ledger.service.people_by_ids(user_id="nope", person_ids=[yana]) == []


@requires_postgres
async def test_resolved_ref_reaches_the_card_through_people_resolve(ledger: Ledger) -> None:
    yana = await _person(ledger, ledger.owner, "Яна Петрова", "жена")
    await _person(ledger, ledger.owner, "Яна Коваль", "коллега")
    tools = MemoryTools(ledger.service, SqlRunPrivacy(ledger.factory))
    sent: list[JsonObject] = []

    async def handler(context: ToolContext, arguments: JsonObject) -> CoreToolResult:
        sent.append(dict(arguments))
        return CoreToolResult(content="ok")

    core = CoreToolRegistry(tools.core_tools())
    core.register(
        CoreTool("people.message", "Message a person.", TO_SCHEMA, "write_external", handler)
    )
    router = McpToolRouter(
        registry=PluginRegistry(StaticInstallationReader({}), StaticManifestSource([])),
        connector=McpConnector(),
        audit=InMemoryToolAudit(),
        approvals=InMemoryApprovalService(),
        core_tools=core,
        config=RouterConfig(call_timeout_s=5, list_cache_s=0),
    )
    router.bind_ref_names(MemoryRefNames(ledger.service, tools.contacts))

    owner = ToolContext(user_id=ledger.owner, run_id=str(uuid.uuid4()), profile="chat")
    resolved = await router.call(owner, call("people.resolve", {"name": "Яна"}))
    assert resolved.structured_content is not None
    candidates: list[dict[str, Any]] = resolved.structured_content["candidates"]  # type: ignore[assignment]
    refs = {c["name"]: c["ref"] for c in candidates}
    assert refs["Яна Петрова"] == yana

    pending = await router.call(owner, call("people.message", {"to": yana, "text": TEXT}))
    assert pending.approval is not None
    summary = pending.approval.summary
    assert summary == f'people: people.message — to=«Яна Петрова (жена)», text="{TEXT}"'
    assert "person:" not in summary and "+375" not in summary
    assert pending.approval.args_hash == canonical_args_hash({"to": yana, "text": TEXT})

    # A stranger's run passing the owner's ref sees only the ref, never the owner's name.
    stranger = ToolContext(user_id=ledger.stranger, run_id=str(uuid.uuid4()), profile="chat")
    theirs = await router.call(stranger, call("people.message", {"to": yana, "text": TEXT}))
    assert theirs.approval is not None
    assert "Яна" not in theirs.approval.summary and yana in theirs.approval.summary
    assert sent == []
