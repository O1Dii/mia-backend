"""Scenario 5: a background job asked by voice and the «пока вас не было» message.

The phone opens a new chat with the side key and asks by voice for a long report «…и пришли,
когда будет готово». The model starts ``background.start`` and answers with a short spoken
ack. While the user talks in another chat, the worker runs the job; its model drafts the
report through ``notify`` and closes with «Отчёт готов…» (UC-MW5b-live): there is still exactly
one result — one inbox item with the full report, one push, one card in the chat where the job
was asked. The next interactive run hears «Пока вас не было…» once, the one after does not.
Whether a live model really chooses ``background.start`` here is UC-M73-1 / UC-MW5b-R1; a
live push needs FCM on the server (UC-M24-1).
"""

from __future__ import annotations

from typing import Any

from tests.e2e_mvp.harness import (
    Mvp,
    Person,
    call,
    final,
    payloads,
    requires_stack,
    say,
    types,
)
from tests.e2e_mvp.s04_support import push_token

pytestmark = requires_stack

GOAL = "Сделай подробный отчёт по рынку электромобилей и пришли, когда будет готово"
REPORT = "## Рынок электромобилей\n\nПродажи выросли на 25%. Лидеры: BYD и Tesla."
AWAY = "Пока пользователя не было"


async def new_chat(phone: Person) -> str:
    """The side key on Android: a new empty chat."""
    created = await phone.post("/v1/chats")
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def feed(phone: Person, chat_id: str) -> list[dict[str, Any]]:
    page = await phone.get("/v1/feed", conversation_id=chat_id)
    assert page.status_code == 200, page.text
    return list(page.json()["items"])


async def test_voice_job_delivers_one_result_to_its_chat_and_is_announced_once(
    mvp: Mvp,
) -> None:
    phone = await mvp.phone("ann")
    await phone.register_device_tools()
    await push_token(phone, "fcm-token-ann")

    # 1. Voice in a new chat: the job starts, the spoken answer is a short ack.
    source = await new_chat(phone)
    mvp.model.then(
        call("background.start", {"goal": GOAL}),
        say("Запустила, пришлю, когда будет готово."),
    )
    events = await phone.run(GOAL, profile="voice", conversation_id=source)
    assert "background.start" in mvp.model.offered(-2)
    [created] = payloads(events, "job.created")
    job_id = created["job_id"]
    assert created["goal"] == GOAL
    assert final(events)["speech"] == "Запустила, пришлю, когда будет готово."
    assert types(events)[-1] == "run.completed"
    jobs = (await phone.get("/v1/jobs")).json()["items"]
    assert [j["id"] for j in jobs] == [job_id]
    assert jobs[0]["origin"]["type"] in {"run", "chat", "user"}

    # 2. The side key again: a new chat and a quick question while the job waits.
    other = await new_chat(phone)
    mvp.model.then(say("Сегодня суббота."))
    await phone.run("Какой сегодня день?", profile="voice", conversation_id=other)
    assert AWAY not in mvp.model.system_text(-1)

    # 3. The worker runs the job: notify drafts the report, the final line is only an ack.
    mvp.model.then(
        call("notify", {"text": REPORT, "title": "Отчёт готов"}),
        say("Отчёт готов, уведомление отправлено."),
    )
    worker = mvp.worker()
    assert await worker.tick_jobs() == 1
    await worker.wait_idle()
    assert AWAY not in mvp.model.system_text(-1)  # job runs never consume the note
    job = (await phone.get(f"/v1/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", job
    delivered = 0
    for _ in range(3):
        delivered += await worker.tick_deliveries()
    assert delivered == 1

    # Exactly one result: one inbox item with the whole report, one push.
    [item] = (await phone.get("/v1/inbox")).json()["items"]
    assert item["source"] == {"type": "job", "id": job_id}
    assert REPORT in item["text"] and "уведомление отправлено" not in item["text"]
    assert (await phone.get(f"/v1/inbox/{item['id']}")).json()["text"] == item["text"]
    [push] = mvp.fcm.sent
    assert push["token"] == "fcm-token-ann"
    assert push["data"] == {**push["data"], "type": "inbox_item", "inbox_item_id": item["id"]}

    # One card, in the chat where the job was asked, naming the inbox item; none elsewhere.
    cards = [i for i in await feed(phone, source) if i.get("card")]
    assert len(cards) == 1
    assert cards[0]["card"]["job_id"] == job_id
    assert cards[0]["card"]["inbox_item_id"] == item["id"]
    assert REPORT.split("\n")[-1] in cards[0]["text"]
    assert [i for i in await feed(phone, other) if i.get("card")] == []

    # 4. The next interactive run hears «Пока вас не было» once; the one after does not.
    mvp.model.then(say("Пока вас не было: отчёт по электромобилям готов. Чем помочь?"))
    events = await phone.run("Привет", profile="voice", conversation_id=other)
    note = mvp.model.system_text(-1)
    assert AWAY in note and item["title"] in note
    assert final(events)["speech"].startswith("Пока вас не было")
    mvp.model.then(say("Пожалуйста."))
    await phone.run("Спасибо", profile="voice", conversation_id=other)
    assert AWAY not in mvp.model.system_text(-1)

    # Nothing was delivered twice after all of this.
    for _ in range(3):
        await worker.tick_deliveries()
    assert len(mvp.fcm.sent) == 1
    assert len((await phone.get("/v1/inbox")).json()["items"]) == 1
