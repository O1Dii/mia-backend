"""Chat retention (ADR-0017 §6): only old unpinned chats go; memory and legacy data stay."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.core.crypto import MasterKeyring
from app.modules.conversations.chats import ChatRetention, scrub_actions
from app.modules.conversations.models import Conversation
from app.modules.conversations.services import ConversationService
from app.modules.memory import MemoryCipher, MemoryService
from app.modules.memory.models import MemoryFact
from app.modules.models.vault import KeyVault
from app.modules.runs.models import RunRow
from sqlalchemy import func, select

from tests.chats.conftest import ChatWorld, check, create_chat
from tests.workspace.harness import key, requires_postgres

pytestmark = requires_postgres

KEYRING = MasterKeyring({"test-1": bytes(range(32))}, "test-1")
NOW = datetime.now(UTC)


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


async def test_retention_deletes_only_old_unpinned_chats(world: ChatWorld) -> None:
    memory = MemoryService(world.factory, MemoryCipher(KeyVault(KEYRING)))
    async with world.harness.client() as client, world.harness.client() as other:
        headers, user = await world.login(client, "alice")
        bob_headers, bob = await world.login(other, "bob")

        async def chat(title: str, *, owner: dict[str, str] = headers) -> uuid.UUID:
            http = client if owner is headers else other
            return uuid.UUID((await create_chat(http, owner, title))["id"])

        old = await chat("старый")
        old_pinned = await chat("старый закреплённый")
        recent = await chat("свежий")
        empty_old = await chat("пустой старый")
        empty_new = await chat("пустой новый")
        bob_old = await chat("старый у Боба", owner=bob_headers)
        bob_forty = await chat("40 дней у Боба", owner=bob_headers)
        for chat_id in (old, old_pinned, recent, bob_old, bob_forty):
            await world.ask(
                user if chat_id not in (bob_old, bob_forty) else bob, "x", conversation_id=chat_id
            )
        old_run = await world.runs.store.get((await _run_of(world, old)), user_id=user)
        assert old_run is not None
        await client.patch(
            f"/v1/chats/{old_pinned}", json={"pinned": True}, headers={**headers, **key()}
        )
        await world.set_times(old, last_message_at=ago(61))
        await world.set_times(old_pinned, last_message_at=ago(400))
        await world.set_times(recent, last_message_at=ago(59))
        await world.set_times(empty_old, created_at=ago(2))
        await world.set_times(empty_new, created_at=ago(0.5))
        await world.set_times(bob_old, last_message_at=ago(400))
        await world.set_times(bob_forty, last_message_at=ago(40))
        legacy = await ConversationService(world.factory).create_conversation(
            user_id=user, idempotency_key="legacy-conversation-2", title="legacy"
        )
        await world.set_times(legacy.value.id, created_at=ago(500), last_message_at=ago(500))
        hidden = await world.chats.background_conversation_id(user_id=user)
        await world.set_times(hidden, last_message_at=ago(90))
        fact, _ = await memory.create_fact(user_id=str(user), text="Я люблю Рим", category="other")

        # Bob keeps chats forever, then switches to 30 days.
        await other.put(
            "/v1/chats/settings", json={"retention_days": None}, headers={**bob_headers, **key()}
        )
        report = await ChatRetention(world.factory, clock=lambda: NOW).sweep()
        assert report.chats == 3  # old, empty_old, hidden
        assert {await _exists(world, c) for c in (old, empty_old, hidden)} == {False}
        for kept in (old_pinned, recent, empty_new, bob_old, bob_forty, legacy.value.id):
            assert await _exists(world, kept), kept

        await other.put(
            "/v1/chats/settings", json={"retention_days": 30}, headers={**bob_headers, **key()}
        )
        report = await ChatRetention(world.factory, clock=lambda: NOW).sweep()
        assert report.chats == 2
        assert not await _exists(world, bob_old) and not await _exists(world, bob_forty)

        # Memory is untouched; the run of a deleted chat stays, unlinked from it.
        async with world.factory() as session:
            facts = await session.scalar(
                select(func.count()).select_from(MemoryFact).where(MemoryFact.user_id == user)
            )
            run = await session.get(RunRow, old_run.id)
        assert facts == 1 and fact.id
        assert run is not None and run.conversation_id is None
        listing = (await client.get("/v1/chats", headers=headers)).json()
        check("ChatPage", listing)
        assert {item["title"] for item in listing["items"]} == {
            "старый закреплённый",
            "свежий",
            "пустой новый",
        }


async def test_expired_links_are_scrubbed_from_answers(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        chat_id = uuid.UUID((await create_chat(client, headers))["id"])
        await world.ask(user, "Подключи Telegram", conversation_id=chat_id)
        expired = {
            "kind": "open_url",
            "url": "https://t.me/mia_bot?start=secret-token-value",
            "label": "Открыть Telegram",
            "expires_at": (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        }
        alive = {
            **expired,
            "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }
        entries = await world.entries(chat_id)
        answer = entries[-1]
        async with world.factory() as session, session.begin():
            row = await session.get(type(answer), answer.id)
            assert row is not None
            row.content = {**row.content, "actions": [expired, alive]}
        report = await ChatRetention(world.factory, clock=lambda: NOW).sweep()
        assert report.actions == 1
        stored = (await world.entries(chat_id))[-1].content["actions"]
        assert stored[0] == {
            "kind": "open_url",
            "label": "Открыть Telegram",
            "expires_at": expired["expires_at"],
            "expired": True,
        }
        assert stored[1]["url"] == alive["url"]
        assert "secret-token-value" not in str(stored[0])
        feed = (
            await client.get("/v1/feed", params={"conversation_id": str(chat_id)}, headers=headers)
        ).json()
        check("FeedPage", feed)
        actions = feed["items"][0]["actions"]
        assert actions[0]["expired"] is True and "url" not in actions[0]
        assert actions[1]["url"] == alive["url"]
        again = await ChatRetention(world.factory, clock=lambda: NOW).sweep()
        assert again.actions == 0


def test_scrub_actions_keeps_unexpired_and_foreign_shapes() -> None:
    assert scrub_actions(None, NOW) is None
    assert scrub_actions([{"kind": "open_url", "url": "https://a.test", "label": "x"}], NOW) is None
    assert scrub_actions(["junk"], NOW) is None


async def _exists(world: ChatWorld, conversation_id: uuid.UUID) -> bool:
    async with world.factory() as session:
        return await session.get(Conversation, conversation_id) is not None


async def _run_of(world: ChatWorld, conversation_id: uuid.UUID) -> uuid.UUID:
    async with world.factory() as session:
        found = await session.scalar(
            select(RunRow.id).where(RunRow.conversation_id == conversation_id).limit(1)
        )
    assert found is not None
    return found
