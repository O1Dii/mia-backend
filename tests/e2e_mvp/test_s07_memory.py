"""Scenario 7: memory — view, delete, incognito, account deletion (production composition).

«Запомни, что Яна — моя жена» by voice → ``memory.remember`` → a suggested fact → confirmed in
the browser → the same memory on the phone and in the next run's context; deleting a fact,
«Забыть всё», an incognito run. Account deletion: the service-level crypto-shred and the user
path ``DELETE /v1/account`` (M17-R1; web «Ещё» and Android settings call it). Incognito: the API
and the web chat's toggle (M17-R1).
"""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path

from tests.e2e_mvp.harness import (
    ROOT,
    Mvp,
    Person,
    call,
    payloads,
    requires_stack,
    say,
    types,
)

pytestmark = requires_stack

YANA = {"text": "Яна — моя жена", "category": "home", "person": {"name": "Яна", "relation": "жена"}}


def context_of(mvp: Mvp, index: int = -1) -> str:
    """What the model got as instructions + memory context in a request."""
    return mvp.model.system_text(index)


async def exported_text(person: Person) -> str:
    archive = await person.get("/v1/memory/export")
    assert archive.status_code == 200, archive.text
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        return "\n".join(
            zipped.read(name).decode("utf-8") for name in zipped.namelist() if name.endswith(".md")
        )


async def test_remember_confirm_view_everywhere_delete_and_forget(mvp: Mvp) -> None:
    phone = await mvp.phone("ann")
    web = await mvp.browser("ann")
    assert web.user_id == phone.user_id

    # Voice: the model proposes a fact; nothing is active until the user confirms.
    mvp.model.then(call("memory.remember", YANA), say("Предлагаю запомнить, подтвердите."))
    events = await phone.run("Запомни, что Яна — моя жена", profile="voice")
    assert "memory.remember" in mvp.model.offered(0)
    [suggested] = payloads(events, "memory.suggested")
    assert suggested["text"] == "Яна — моя жена"
    assert (await web.get("/v1/memory/people")).json()["items"] == []

    # The browser («Что Мия знает» → «Предложено» → «Подтвердить»).
    pending = (await web.get("/v1/memory/facts", status="suggested")).json()["items"]
    assert [f["id"] for f in pending] == [suggested["fact_id"]]
    confirmed = await web.patch(f"/v1/memory/facts/{suggested['fact_id']}", {"status": "active"})
    assert confirmed.status_code == 200, confirmed.text
    coffee = await web.post(
        "/v1/memory/facts", {"text": "Пьёт кофе без сахара", "category": "interests"}
    )
    assert coffee.status_code == 201, coffee.text

    # The phone sees the same memory (one account), and the next run gets it as context.
    facts = {f["text"]: f for f in (await phone.get("/v1/memory/facts")).json()["items"]}
    assert {"Яна — моя жена", "Пьёт кофе без сахара"} <= set(facts)
    people = (await phone.get("/v1/memory/people")).json()["items"]
    assert [(p["display_name"], p["relation"]) for p in people] == [("Яна", "жена")]
    # The context is the pinned profile plus the facts relevant to the question (Postgres FTS).
    mvp.model.then(say("Помню: Яна — ваша жена, а кофе вы пьёте без сахара."))
    await phone.run("Кто такая Яна и какой кофе я пью?", profile="voice")
    assert "Яна (жена)" in context_of(mvp) and "кофе без сахара" in context_of(mvp)
    exported = await exported_text(web)
    assert "Пьёт кофе без сахара" in exported and "**Яна** — жена" in exported

    # Delete one fact in the browser: gone from the list, the export and the next context.
    deleted = await web.delete(f"/v1/memory/facts/{coffee.json()['id']}")
    assert deleted.status_code == 204, deleted.text
    assert "Пьёт кофе без сахара" not in {
        f["text"] for f in (await phone.get("/v1/memory/facts")).json()["items"]
    }
    assert "кофе" not in await exported_text(phone)
    mvp.model.then(say("Ок."))
    await phone.run("Кто такая Яна и какой кофе я пью?", profile="voice")
    assert "кофе" not in context_of(mvp) and "Яна (жена)" in context_of(mvp)

    # The access log shows reads without contents.
    log = (await web.get("/v1/memory/access-log")).json()["items"]
    assert log and all("text" not in entry for entry in log)
    assert {"context", "erase"} & {entry["action"] for entry in log} == {"context"}

    # «Забыть всё»: facts and people are gone; the next run has no memory context.
    forgot = await web.delete("/v1/memory")
    assert forgot.status_code == 204, forgot.text
    assert (await phone.get("/v1/memory/facts")).json()["items"] == []
    assert (await phone.get("/v1/memory/people")).json()["items"] == []
    mvp.model.then(say("Пока ничего не знаю."))
    await phone.run("Кто такая Яна?", profile="voice")
    assert "Яна" not in context_of(mvp)


async def test_incognito_run_remembers_nothing_and_leaves_no_history(mvp: Mvp) -> None:
    phone = await mvp.phone("bob")
    created = await phone.post(
        "/v1/memory/facts", {"text": "Пьёт кофе без сахара", "category": "interests"}
    )
    assert created.status_code == 201, created.text
    secret_input = "Запомни, что я люблю чай — инкогнито-проверка"

    mvp.model.then(
        call("memory.remember", {"text": "Любит чай", "category": "interests"}),
        say("В режиме инкогнито я ничего не запоминаю."),
    )
    run_id = await phone.start_run(secret_input, profile="chat", incognito=True)
    events = await phone.events(run_id)
    assert types(events)[-1] == "run.completed"
    assert "memory.suggested" not in types(events)
    [done] = payloads(events, "tool.call.completed")
    assert done["status"] == "failed" and done["error"]["code"] == "memory_incognito"
    assert "кофе" not in context_of(mvp, 0)  # no memory goes out in incognito

    facts = (await phone.get("/v1/memory/facts")).json()["items"]
    assert [f["text"] for f in facts] == ["Пьёт кофе без сахара"]
    run = (await phone.get(f"/v1/runs/{run_id}")).json()
    assert "conversation_id" not in run
    [row] = await mvp.sql(
        "SELECT incognito, input_text FROM runs WHERE id = :id", id=uuid.UUID(run_id)
    )
    assert row.incognito is True and row.input_text is None
    feed = (await phone.get("/v1/feed")).json()
    assert secret_input not in str(feed) and "инкогнито" not in str(feed)


async def test_account_deletion_crypto_shreds_memory_at_the_service_level(mvp: Mvp) -> None:
    """``MemoryService.shred_account`` (M17), the step ``DELETE /v1/account`` relies on."""
    phone = await mvp.phone("carol")
    added = await phone.post(
        "/v1/memory/facts", {"text": "Аллергия на орехи", "category": "health"}
    )
    assert added.status_code == 201, added.text
    [keys] = await mvp.sql(
        "SELECT count(*) AS n FROM user_data_keys WHERE user_id = :u", u=uuid.UUID(phone.user_id)
    )
    assert keys.n == 1

    assert await mvp.app.state.memory.service.shred_account(user_id=phone.user_id) is True

    [after] = await mvp.sql(
        "SELECT (SELECT count(*) FROM user_data_keys WHERE user_id = :u) AS keys, "
        "(SELECT count(*) FROM memory_facts WHERE user_id = :u) AS facts",
        u=uuid.UUID(phone.user_id),
    )
    assert (after.keys, after.facts) == (0, 0)


async def test_the_user_can_delete_the_account(mvp: Mvp) -> None:
    phone = await mvp.phone("dave")
    added = await phone.post("/v1/memory/facts", {"text": "Живёт в Минске", "category": "profile"})
    assert added.status_code == 201
    deleted = await phone.delete("/v1/account")
    assert deleted.status_code == 204, deleted.status_code
    assert (await phone.get("/v1/workspace")).status_code == 401
    [left] = await mvp.sql(
        "SELECT count(*) AS n FROM users WHERE id = :u", u=uuid.UUID(phone.user_id)
    )
    assert left.n == 0


def test_the_web_chat_can_start_an_incognito_run() -> None:
    sources = "\n".join(
        path.read_text("utf-8") for path in Path(ROOT / "web" / "src").rglob("*.ts*")
    )
    assert "incognito" in sources
