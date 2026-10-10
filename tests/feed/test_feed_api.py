"""``GET /v1/feed``: one feed for voice and chat, keyset paging, isolation, auth (PostgreSQL)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.modules.conversations.feed import FeedCursor, FeedItem
from app.modules.conversations.models import Conversation, Entry, Modality, TurnSource
from app.modules.runs import NewRun, SqlRunStore
from httpx import AsyncClient
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from sqlalchemy import select

from tests.fakes import assert_mvp_valid, text_step
from tests.fakes.contracts import validator_module
from tests.feed.conftest import FeedWorld
from tests.runs.conftest import RunWorld, collect
from tests.runs.gateways import answer, scripted
from tests.workspace.harness import requires_postgres

pytestmark = requires_postgres

OPENAPI = validator_module().load_mvp_openapi()
PAGE_VALIDATOR = Draft202012Validator(
    {"$ref": "#/components/schemas/FeedPage", "components": OPENAPI["components"]},
    format_checker=FormatChecker(),
)
ERROR_VALIDATOR = Draft202012Validator(
    {"$ref": "#/components/schemas/ErrorEnvelope", "components": OPENAPI["components"]}
)


def check_page(body: dict[str, Any]) -> None:
    errors = [error.message for error in PAGE_VALIDATOR.iter_errors(body)]
    assert not errors, errors
    for item in body["items"]:
        if item["display"] is not None:  # same shape as AssistantResponse.display
            assert_mvp_valid(
                {"speech": "Готово.", "display": item["display"], "followups": []},
                "assistant-response",
            )


async def walk(
    client: AsyncClient, headers: dict[str, str], *, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """Follow ``next_cursor`` to the end; returns the items and the number of pages."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["before"] = cursor
        response = await client.get("/v1/feed", params=params, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        check_page(body)
        pages += 1
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items, pages
        assert len(body["items"]) == limit


async def test_voice_and_chat_share_one_feed_newest_first(world: FeedWorld) -> None:
    async with world.harness.client() as client:
        headers, user_id = await world.alice_bearer(client, "sub-alice")
        runs = RunWorld(
            store=SqlRunStore(world.harness.factory),
            users=[user_id],
            session_factory=world.harness.factory,
        )
        service = runs.service(
            scripted(
                text_step(answer("Записала.", {"kind": "text", "markdown": "Список: молоко"})),
                text_step(answer("Молоко.")),
            ),
            with_feed=True,
        )
        voice, _ = await service.start(
            NewRun(user_id=user_id, profile="voice", input_text="Запомни молоко")
        )
        await collect(service, user_id, voice.id)
        chat, _ = await service.start(
            NewRun(user_id=user_id, profile="chat", input_text="Что купить?")
        )
        await collect(service, user_id, chat.id)

        response = await client.get("/v1/feed", headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        check_page(body)
        assert body["next_cursor"] is None
        items = body["items"]
        assert [(i["role"], i["source"], i["run_id"]) for i in items] == [
            ("assistant", "run_chat", str(chat.id)),
            ("user", "run_chat", str(chat.id)),
            ("assistant", "run_voice", str(voice.id)),
            ("user", "run_voice", str(voice.id)),
        ]
        assert items[1]["text"] == "Что купить?" and items[0]["text"] == "Молоко."
        assert items[1]["display"] is None and items[3]["display"] is None
        assert items[0]["display"] is None
        assert items[2]["display"] == {"kind": "text", "markdown": "Список: молоко"}
        assert items[2]["text"] == "Список: молоко"
        assert len({i["id"] for i in items}) == 4


async def test_incognito_runs_are_not_in_the_feed(world: FeedWorld) -> None:
    async with world.harness.client() as client:
        headers, user_id = await world.alice_bearer(client, "sub-alice")
        runs = RunWorld(
            store=SqlRunStore(world.harness.factory),
            users=[user_id],
            session_factory=world.harness.factory,
        )
        service = runs.service(
            scripted(text_step(answer("Секрет.")), text_step(answer("Обычный."))), with_feed=True
        )
        hidden, _ = await service.start(
            NewRun(user_id=user_id, profile="chat", input_text="тайный вопрос", incognito=True)
        )
        await collect(service, user_id, hidden.id)
        empty = await client.get("/v1/feed", headers=headers)
        assert empty.json()["items"] == [] and empty.json()["next_cursor"] is None

        shown, _ = await service.start(
            NewRun(user_id=user_id, profile="chat", input_text="обычный вопрос")
        )
        await collect(service, user_id, shown.id)
        items = (await client.get("/v1/feed", headers=headers)).json()["items"]
        assert {i["run_id"] for i in items} == {str(shown.id)}
        assert all("тайный" not in i["text"] and "Секрет" not in i["text"] for i in items)
    async with world.harness.factory() as session:
        stored = (await session.scalars(select(Entry))).all()
    assert all("тайный" not in str(entry.content) for entry in stored)


async def test_pagination_covers_every_item_once(world: FeedWorld) -> None:
    async with world.harness.client() as client:
        headers, user_id = await world.alice_bearer(client, "sub-alice")
        for index in range(13):
            await world.exchange(
                user_id,
                f"m{index}",
                source=TurnSource.RUN_VOICE if index % 2 else TurnSource.RUN_CHAT,
                display={"kind": "text", "markdown": f"m{index}"} if index % 3 == 0 else None,
            )
        everything, _ = await walk(client, headers, limit=100)
        assert len(everything) == 26
        paged, pages = await walk(client, headers, limit=7)
        assert pages == 4
        assert [i["id"] for i in paged] == [i["id"] for i in everything]
        assert len({i["id"] for i in paged}) == 26
        # exact multiple of the page size ends with an empty-cursor page, not an extra one
        _, pages = await walk(client, headers, limit=13)
        assert pages == 2
        first = (await client.get("/v1/feed", params={"limit": 26}, headers=headers)).json()
        assert first["next_cursor"] is None and len(first["items"]) == 26
        assert paged[0]["text"] == "m12: answer" and paged[-1]["text"] == "m0: question"


async def test_pages_stay_exact_while_writers_append(world: FeedWorld) -> None:
    async with world.harness.client() as client:
        _, user_id = await world.alice_bearer(client, "sub-alice")
    for index in range(20):
        await world.exchange(user_id, f"seed{index}")
    initial = {i.id for i in (await world.feed.history(user_id=user_id, limit=100)).items}
    assert len(initial) == 40

    stop = asyncio.Event()

    async def writer(name: int) -> None:
        count = 0
        while not stop.is_set():
            await world.exchange(user_id, f"w{name}-{count}")
            count += 1

    writers = [asyncio.create_task(writer(n)) for n in range(3)]
    try:
        for _ in range(3):
            collected: list[FeedItem] = []
            cursor: str | None = None
            while True:
                page = await world.feed.history(user_id=user_id, before=cursor, limit=5)
                collected.extend(page.items)
                await asyncio.sleep(0.01)  # let writers land between the pages
                cursor = page.next_cursor
                if cursor is None:
                    break
            sequences = [item.sequence for item in collected]
            # contiguous descending run down to 1: no gap, no duplicate, nothing reordered
            assert sequences == list(range(sequences[0], 0, -1))
            assert initial <= {item.id for item in collected}
    finally:
        stop.set()
        await asyncio.gather(*writers)
    final = await world.feed.history(user_id=user_id, limit=100)
    assert final.next_cursor is None or len(final.items) == 100
    sequences = [item.sequence for item in final.items]
    assert sequences == list(range(sequences[0], sequences[0] - len(sequences), -1))


async def test_each_user_sees_only_their_own_feed_and_cursors(world: FeedWorld) -> None:
    async with world.harness.client() as alice, world.harness.client() as bob:
        alice_headers, alice_id = await world.alice_bearer(alice, "sub-alice")
        bob_headers, bob_id = await world.alice_bearer(bob, "sub-bob")
        for index in range(6):
            await world.exchange(alice_id, f"alice{index}")
            await world.exchange(bob_id, f"bob{index}")

        alice_items, _ = await walk(alice, alice_headers, limit=4)
        bob_items, _ = await walk(bob, bob_headers, limit=4)
        assert len(alice_items) == len(bob_items) == 12
        assert all(i["text"].startswith("alice") for i in alice_items)
        assert all(i["text"].startswith("bob") for i in bob_items)
        assert not {i["id"] for i in alice_items} & {i["id"] for i in bob_items}
        assert not {i["run_id"] for i in alice_items} & {i["run_id"] for i in bob_items}

        alice_cursor = (
            await alice.get("/v1/feed", params={"limit": 2}, headers=alice_headers)
        ).json()["next_cursor"]
        stolen = await bob.get("/v1/feed", params={"before": alice_cursor}, headers=bob_headers)
        assert stolen.status_code == 400
        assert stolen.json()["error"]["code"] == "invalid_feed_cursor"
        assert not list(ERROR_VALIDATOR.iter_errors(stolen.json()))
        assert stolen.json()["error"]["retryable"] is False
        assert "alice" not in stolen.text

        # a hand-made cursor aimed at the other user's feed is rejected the same way
        bob_feed = await world.feed.primary_feed_id(user_id=bob_id)
        forged = FeedCursor(bob_feed, 10**6).encode()
        assert (
            await alice.get("/v1/feed", params={"before": forged}, headers=alice_headers)
        ).status_code == 400


async def test_empty_feed_and_bad_input(world: FeedWorld) -> None:
    async with world.harness.client() as client:
        headers, user_id = await world.alice_bearer(client, "sub-alice")
        empty = await client.get("/v1/feed", headers=headers)
        assert empty.status_code == 200 and empty.json() == {
            "items": [],
            "next_cursor": None,
            "conversation_id": None,
        }
        # a read never creates the feed
        async with world.harness.factory() as session:
            assert (
                await session.scalars(select(Conversation).where(Conversation.user_id == user_id))
            ).first() is None
        forged = FeedCursor(uuid.uuid4(), 3).encode()
        for value in (forged, "garbage"):
            response = await client.get("/v1/feed", params={"before": value}, headers=headers)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_feed_cursor"
        for params in ({"limit": 0}, {"limit": 101}, {"limit": "x"}, {"before": ""}):
            response = await client.get("/v1/feed", params=params, headers=headers)
            assert response.status_code == 422, params
            assert response.json()["error"]["code"] == "validation_error"
            assert not list(ERROR_VALIDATOR.iter_errors(response.json()))


async def test_bearer_and_web_cookie_read_the_same_feed(world: FeedWorld) -> None:
    async with world.harness.client() as android, world.harness.client() as browser:
        headers, android_id = await world.alice_bearer(android, "sub-alice")
        web_id = await world.web_user(browser, "sub-alice")
        assert android_id == web_id
        await world.exchange(
            android_id, "shared", source=TurnSource.RUN_VOICE, modality=Modality.AUDIO
        )
        via_bearer = await android.get("/v1/feed", headers=headers)
        via_cookie = await browser.get("/v1/feed")
        assert via_bearer.status_code == via_cookie.status_code == 200
        assert via_bearer.json() == via_cookie.json()
        assert [i["source"] for i in via_cookie.json()["items"]] == ["run_voice", "run_voice"]

    async with world.harness.client() as stranger:
        await world.web_user(stranger, "sub-mallory")
        assert (await stranger.get("/v1/feed")).json() == {
            "items": [],
            "next_cursor": None,
            "conversation_id": None,
        }


async def test_unauthenticated_requests_are_rejected(world: FeedWorld) -> None:
    async with world.harness.client() as client:
        missing = await client.get("/v1/feed")
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "authentication_required"
        assert not list(ERROR_VALIDATOR.iter_errors(missing.json()))
        bad = await client.get("/v1/feed", headers={"Authorization": "Bearer not-a-token"})
        assert bad.status_code == 401
        assert not list(ERROR_VALIDATOR.iter_errors(bad.json()))
        assert bad.headers["x-request-id"]


async def test_screen_actions_are_listed_with_the_answer_until_they_expire(
    world: FeedWorld,
) -> None:
    """M41-R1: a tool's screen-only action rides on the answer; after expiry it has no URL."""
    live = {
        "kind": "open_url",
        "url": "https://t.me/mia_test_bot?start=LIVE",
        "label": "Открыть Telegram",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    old = {
        **live,
        "url": "https://t.me/mia_test_bot?start=OLD",
        "expires_at": "2020-01-01T00:00:00Z",
    }
    async with world.harness.client() as client:
        headers, user_id = await world.alice_bearer(client, "sub-actions")
        await world.exchange(user_id, "old", actions=(old,))
        await world.exchange(user_id, "live", actions=(live,))
        await world.exchange(user_id, "plain")

        body = (await client.get("/v1/feed", headers=headers)).json()

    check_page(body)
    plain, _, fresh, _, stale, _ = body["items"]
    assert "actions" not in plain
    assert fresh["actions"] == [live]
    assert stale["actions"] == [
        {
            "kind": "open_url",
            "label": "Открыть Telegram",
            "expires_at": "2020-01-01T00:00:00Z",
            "expired": True,
        }
    ]
    assert "OLD" not in str(body)
