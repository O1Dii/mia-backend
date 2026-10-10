"""``/v1/chats*``: lifecycle, order, paging, isolation, settings, «continue» (PostgreSQL)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.modules.conversations.services import ConversationService

from tests.chats.conftest import ChatWorld, check, create_chat
from tests.workspace.harness import key, requires_postgres, web_headers, web_login

pytestmark = requires_postgres


async def test_create_rename_pin_delete(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        first = await create_chat(client, headers)
        assert first["kind"] == "chat" and first["title"] is None and first["pinned"] is False
        second = await create_chat(client, headers, "Отпуск")
        assert (second["title"], second["title_source"]) == ("Отпуск", "user")

        listing = (await client.get("/v1/chats", headers=headers)).json()
        check("ChatPage", listing)
        assert [chat["id"] for chat in listing["items"]] == [second["id"], first["id"]]

        pinned = await client.patch(
            f"/v1/chats/{first['id']}", json={"pinned": True}, headers={**headers, **key()}
        )
        check("Chat", pinned.json())
        assert pinned.json()["pinned"] is True and pinned.json()["pinned_at"] is not None
        listing = (await client.get("/v1/chats", headers=headers)).json()
        assert [chat["id"] for chat in listing["items"]] == [first["id"], second["id"]]

        renamed = await client.patch(
            f"/v1/chats/{first['id']}", json={"title": "Работа"}, headers={**headers, **key()}
        )
        assert (renamed.json()["title"], renamed.json()["title_source"]) == ("Работа", "user")
        reset = await client.patch(
            f"/v1/chats/{first['id']}", json={"title": None}, headers={**headers, **key()}
        )
        assert (reset.json()["title"], reset.json()["title_source"]) == (None, None)
        unpinned = await client.patch(
            f"/v1/chats/{first['id']}", json={"pinned": False}, headers={**headers, **key()}
        )
        assert unpinned.json()["pinned"] is False

        empty = await client.patch(
            f"/v1/chats/{first['id']}", json={}, headers={**headers, **key()}
        )
        assert empty.status_code == 422
        no_key = await client.patch(
            f"/v1/chats/{first['id']}", json={"pinned": True}, headers=headers
        )
        assert no_key.status_code == 422

        deleted = await client.delete(f"/v1/chats/{first['id']}", headers={**headers, **key()})
        assert deleted.status_code == 204
        gone = await client.get(f"/v1/chats/{first['id']}", headers=headers)
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "conversation_not_found"
        listing = (await client.get("/v1/chats", headers=headers)).json()
        assert [chat["id"] for chat in listing["items"]] == [second["id"]]
        assert await world.conversation(uuid.UUID(first["id"])) is None
        assert user is not None


async def test_create_is_idempotent(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, _ = await world.login(client, "alice")
        idem = key()
        one = await client.post("/v1/chats", json={"title": "A"}, headers={**headers, **idem})
        two = await client.post("/v1/chats", json={"title": "A"}, headers={**headers, **idem})
        assert one.json()["id"] == two.json()["id"]
        conflict = await client.post("/v1/chats", json={"title": "B"}, headers={**headers, **idem})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        assert len((await client.get("/v1/chats", headers=headers)).json()["items"]) == 1


async def test_paging_and_isolation(world: ChatWorld) -> None:
    async with world.harness.client() as client, world.harness.client() as other:
        headers, _ = await world.login(client, "alice")
        stranger, stranger_id = await world.login(other, "bob")
        created = [await create_chat(client, headers, f"Чат {index}") for index in range(5)]
        await client.patch(
            f"/v1/chats/{created[1]['id']}", json={"pinned": True}, headers={**headers, **key()}
        )
        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, str | int] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = (await client.get("/v1/chats", params=params, headers=headers)).json()
            check("ChatPage", page)
            seen += [chat["id"] for chat in page["items"]]
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
        expected = [created[1]["id"]] + [c["id"] for c in reversed(created) if c is not created[1]]
        assert seen == expected and pages == 3

        bad = await client.get("/v1/chats", params={"cursor": "nope"}, headers=headers)
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_chat_cursor"

        target = created[0]["id"]
        assert (await other.get(f"/v1/chats/{target}", headers=stranger)).status_code == 404
        patch = await other.patch(
            f"/v1/chats/{target}", json={"title": "x"}, headers={**stranger, **key()}
        )
        assert patch.status_code == 404
        delete = await other.delete(f"/v1/chats/{target}", headers={**stranger, **key()})
        assert delete.status_code == 404
        assert (await other.get("/v1/chats", headers=stranger)).json()["items"] == []
        feed = await other.get("/v1/feed", params={"conversation_id": target}, headers=stranger)
        assert feed.status_code == 404
        assert stranger_id is not None
        assert (await client.get("/v1/chats")).status_code == 401


async def test_legacy_and_hidden_conversations_are_not_chats(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        legacy = await ConversationService(world.factory).create_conversation(
            user_id=user, idempotency_key="legacy-conversation-1", title="Старое"
        )
        hidden = await world.chats.background_conversation_id(user_id=user)
        assert (await client.get("/v1/chats", headers=headers)).json()["items"] == []
        for conversation in (legacy.value.id, hidden):
            assert (
                await client.get(f"/v1/chats/{conversation}", headers=headers)
            ).status_code == 404
            feed = await client.get(
                "/v1/feed", params={"conversation_id": str(conversation)}, headers=headers
            )
            assert feed.status_code == 404


async def test_settings(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, _ = await world.login(client, "alice")
        current = (await client.get("/v1/chats/settings", headers=headers)).json()
        check("ChatSettings", current)
        assert current == {"retention_days": 60, "choices": [30, 60, 180, None]}
        for value in (30, None, 180):
            updated = await client.put(
                "/v1/chats/settings", json={"retention_days": value}, headers={**headers, **key()}
            )
            assert updated.status_code == 200 and updated.json()["retention_days"] == value
            check("ChatSettings", updated.json())
        for bad in (45, "60", True):
            rejected = await client.put(
                "/v1/chats/settings", json={"retention_days": bad}, headers={**headers, **key()}
            )
            assert rejected.status_code == 422
        stored = (await client.get("/v1/chats/settings", headers=headers)).json()
        assert stored["retention_days"] == 180


async def test_web_cookie_reaches_the_same_chats(world: ChatWorld) -> None:
    async with world.harness.client() as phone, world.harness.client() as browser:
        headers, _ = await world.login(phone, "alice")
        chat = await create_chat(phone, headers, "Общий")
        await web_login(browser, "alice")
        listed = (await browser.get("/v1/chats")).json()
        assert [item["id"] for item in listed["items"]] == [chat["id"]]
        cross_site = await browser.post(
            "/v1/chats", headers={**key(), "Origin": "https://evil.test"}
        )
        assert cross_site.status_code in (401, 403)
        created = await browser.post(
            "/v1/chats", json={"title": "Из браузера"}, headers=web_headers()
        )
        assert created.status_code == 201
        assert len((await phone.get("/v1/chats", headers=headers)).json()["items"]) == 2


async def test_continue_offers_the_chat_answered_minutes_ago(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        none = (await client.get("/v1/chats/continue", headers=headers)).json()
        check("ChatContinue", none)
        assert none == {"chat": None, "within_seconds": 300}

        chat = await create_chat(client, headers)
        await world.ask(user, "Поставь таймер", conversation_id=uuid.UUID(chat["id"]))
        found = (await client.get("/v1/chats/continue", headers=headers)).json()
        check("ChatContinue", found)
        assert found["chat"]["id"] == chat["id"] and found["chat"]["last_assistant_at"]

        await world.set_times(
            uuid.UUID(chat["id"]), last_assistant_at=datetime.now(UTC) - timedelta(minutes=6)
        )
        assert (await client.get("/v1/chats/continue", headers=headers)).json()["chat"] is None
        wider = await client.get(
            "/v1/chats/continue", params={"within_seconds": 600}, headers=headers
        )
        assert wider.json()["chat"]["id"] == chat["id"]
        assert (
            await client.get("/v1/chats/continue", params={"within_seconds": 5}, headers=headers)
        ).status_code == 422
