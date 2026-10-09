"""Memory Ledger on PostgreSQL: consent, people, encryption at rest, context, shred, isolation."""

from __future__ import annotations

import uuid

import pytest
from app.core.crypto import DecryptionError, open_sealed
from app.core.ports.memory_provider import MemoryProvider, MemorySource
from app.modules.memory.domain import PersonInput
from app.modules.memory.errors import (
    IdempotencyConflictError,
    InvalidMemoryRequestError,
    RecordNotFoundError,
)
from app.modules.memory.models import MemoryAccessLog, MemoryFact, MemoryPerson
from app.modules.models.models import UserDataKey
from app.modules.models.vault import secret_aad
from sqlalchemy import func, select, text

from tests.fakes import assert_mvp_valid
from tests.memory.conftest import MemoryWorld
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres

TURN = MemorySource("turn", ref="run-1")


async def test_service_implements_the_port(mem: MemoryWorld) -> None:
    assert isinstance(mem.service, MemoryProvider)


async def test_remember_suggests_then_confirm_creates_the_person(mem: MemoryWorld) -> None:
    user = str(mem.user)
    fact = await mem.service.remember(
        user_id=user, text="Запомни, что Яна — моя жена", category="home", source=TURN
    )
    assert fact.status == "suggested"
    assert_mvp_valid(fact.contract(), "memory", "Fact")
    # Suggested facts never reach a model or name resolution.
    assert await mem.service.resolve_person(user_id=user, name="Яна") == []
    assert await mem.service.context(user_id=user, query="Яна") == []

    confirmed = await mem.service.update_fact(user_id=user, fact_id=str(fact.id), status="active")
    assert confirmed.status == "active"
    assert confirmed.person_id is not None

    for spoken in ("Яна", "Яне", "жене"):
        found = await mem.service.resolve_person(user_id=user, name=spoken)
        assert [(p.id, p.display_name, p.relation) for p in found] == [
            (str(confirmed.person_id), "Яна", "жена")
        ], spoken
    people = await mem.service.list_people(user_id=user)
    assert len(people) == 1
    assert_mvp_valid(people[0].contract(), "memory", "Person")
    assert people[0].source.type == "turn"


async def test_reject_drops_the_person_draft(mem: MemoryWorld) -> None:
    user = str(mem.user)
    fact = await mem.service.remember(
        user_id=user, text="Олег — мой начальник", category="work", source=TURN
    )
    rejected = await mem.service.update_fact(user_id=user, fact_id=str(fact.id), status="rejected")
    assert rejected.status == "rejected"
    assert await mem.service.list_people(user_id=user) == []


async def test_autosave_only_for_allowed_non_sensitive_categories(mem: MemoryWorld) -> None:
    user = str(mem.user)
    with pytest.raises(InvalidMemoryRequestError):
        await mem.service.update_settings(user_id=user, autosave_categories=["health"])
    await mem.service.update_settings(user_id=user, autosave_categories=["interests"])
    saved = await mem.service.remember(
        user_id=user, text="Любит джаз", category="interests", source=TURN
    )
    pending = await mem.service.remember(
        user_id=user, text="Аллергия на орехи", category="health", source=TURN
    )
    imported = await mem.service.remember(
        user_id=user,
        text="Любит рок",
        category="interests",
        source=MemorySource("app", ref="client"),
    )
    assert (saved.status, pending.status, imported.status) == ("active", "suggested", "suggested")
    assert pending.sensitivity == "sensitive"


async def test_content_is_encrypted_at_rest(mem: MemoryWorld) -> None:
    user = str(mem.user)
    fact, _ = await mem.service.create_fact(
        user_id=user, text="Любимый кофе — флэт уайт", category="interests"
    )
    await mem.service.create_person(
        user_id=user,
        data=PersonInput("Яна", "жена", identifiers={"phone": "+79161234567"}),
    )
    async with mem.factory() as session:
        dump = (
            await session.execute(
                text(
                    "SELECT f.text_ciphertext, f.search_index::text, p.payload_ciphertext "
                    "FROM memory_facts f, memory_people p WHERE f.user_id = :u"
                ),
                {"u": mem.user},
            )
        ).one()
    blob = b"".join([bytes(dump[0]), dump[1].encode(), bytes(dump[2])])
    for secret in ("флэт", "кофе", "Яна", "жена", "79161234567"):
        assert secret.encode() not in blob, secret
    # Stems are not readable in the blind index either.
    assert "коф" not in dump[1]
    assert fact.text == "Любимый кофе — флэт уайт"


async def test_context_is_pinned_profile_plus_fts_and_minimized(mem: MemoryWorld) -> None:
    user = str(mem.user)
    service = mem.service
    await service.create_fact(user_id=user, text="Меня зовут Алексей", category="profile")
    await service.create_fact(
        user_id=user, text="Номер паспорта 1234567890, телефон +79161234567", category="other"
    )
    await service.create_fact(user_id=user, text="Пьёт кофе без сахара", category="interests")
    await service.create_fact(user_id=user, text="Работает в Минске", category="work")
    yana, _ = await service.create_person(
        user_id=user, data=PersonInput("Яна", "жена", identifiers={"phone": "+79161234567"})
    )

    facts = await service.context(user_id=user, query="Свари кофе и позвони Яне")
    texts = [fact.text for fact in facts]
    assert texts[0] == "Меня зовут Алексей"  # pinned profile first
    assert "Пьёт кофе без сахара" in texts  # FTS on the blind index
    assert "Работает в Минске" not in texts  # not relevant to the query
    assert f"person:{yana.id}: Яна (жена)" in texts
    joined = "\n".join(texts)
    assert "79161234567" not in joined

    passport = await service.context(user_id=user, query="какой номер паспорта")
    assert any("скрыт]" in fact.text for fact in passport)
    assert all("1234567890" not in fact.text for fact in passport)

    only_work = await service.context(user_id=user, query="Минске кофе", categories=["work"])
    assert [fact.text for fact in only_work] == ["Работает в Минске"]

    await service.mark_used(user_id=user, record_ids=[f.id for f in facts])
    listed = await service.list_facts(user_id=user, status="active")
    used = {str(f.id) for f in listed.items if f.last_used_at is not None}
    assert used == {f.id for f in facts if f.id != str(yana.id)}
    async with mem.factory() as session:
        actions = (await session.scalars(select(MemoryAccessLog.action))).all()
    assert "context" in actions


async def test_people_identifiers_are_for_the_core_only(mem: MemoryWorld) -> None:
    user = str(mem.user)
    person, _ = await mem.service.create_person(
        user_id=user,
        data=PersonInput("Яна", "жена", identifiers={"telegram_user_id": 42, "phone": "+7916123"}),
    )
    refs = await mem.service.resolve_person(user_id=user, name="Яна")
    assert refs[0].model_ref == f"person:{person.id}"
    assert not hasattr(refs[0], "identifiers")
    ids = await mem.service.person_identifiers(user_id=user, person_id=str(person.id))
    assert ids == {"telegram_user_id": 42, "phone": "+7916123"}
    other = str(mem.users[1])
    assert await mem.service.person_identifiers(user_id=other, person_id=str(person.id)) == {}


async def test_other_users_see_and_change_nothing(mem: MemoryWorld) -> None:
    owner, other = str(mem.users[0]), str(mem.users[1])
    fact, _ = await mem.service.create_fact(user_id=owner, text="Секрет", category="other")
    person, _ = await mem.service.create_person(user_id=owner, data=PersonInput("Яна"))
    assert (await mem.service.list_facts(user_id=other)).items == []
    assert await mem.service.list_people(user_id=other) == []
    assert await mem.service.resolve_person(user_id=other, name="Яна") == []
    for call in (
        mem.service.update_fact(user_id=other, fact_id=str(fact.id), status="rejected"),
        mem.service.delete_fact(user_id=other, fact_id=str(fact.id)),
        mem.service.update_person(user_id=other, person_id=str(person.id), changes={}),
        mem.service.delete_person(user_id=other, person_id=str(person.id)),
        mem.service.delete_fact(user_id=owner, fact_id="not-a-uuid"),
    ):
        with pytest.raises(RecordNotFoundError):
            await call


async def test_idempotent_create(mem: MemoryWorld) -> None:
    user = str(mem.user)
    first, created = await mem.service.create_fact(
        user_id=user, text="Факт", category="other", idempotency_key="k" * 20
    )
    again, created_again = await mem.service.create_fact(
        user_id=user, text="Факт", category="other", idempotency_key="k" * 20
    )
    assert (created, created_again, first.id) == (True, False, again.id)
    with pytest.raises(IdempotencyConflictError):
        await mem.service.create_fact(
            user_id=user, text="Другой", category="other", idempotency_key="k" * 20
        )


async def test_pagination(mem: MemoryWorld) -> None:
    user = str(mem.user)
    for index in range(5):
        await mem.service.create_fact(user_id=user, text=f"Факт {index}", category="other")
    first = await mem.service.list_facts(user_id=user, limit=2)
    second = await mem.service.list_facts(user_id=user, limit=2, cursor=first.next_cursor)
    third = await mem.service.list_facts(user_id=user, limit=2, cursor=second.next_cursor)
    ids = [f.id for page in (first, second, third) for f in page.items]
    assert len(ids) == len(set(ids)) == 5
    assert third.next_cursor is None
    with pytest.raises(InvalidMemoryRequestError):
        await mem.service.list_facts(user_id=user, cursor="%%%")


async def test_crypto_shred_makes_surviving_ciphertext_unreadable(mem: MemoryWorld) -> None:
    user = str(mem.user)
    fact, _ = await mem.service.create_fact(user_id=user, text="Очень личное", category="other")
    async with mem.factory() as session:
        backup = (
            await session.execute(
                select(MemoryFact.text_ciphertext).where(MemoryFact.id == fact.id)
            )
        ).scalar_one()
        old_key = await session.get(UserDataKey, mem.user)
        assert old_key is not None
        dek = mem.vault.keyring.unwrap(
            old_key.master_key_id, old_key.wrapped_dek, context=f"mia.dek.v1|{mem.user}".encode()
        )
    aad = secret_aad(mem.user, "memory.fact.text", fact.id)
    assert open_sealed(dek, backup, aad=aad) == "Очень личное".encode()

    assert await mem.service.shred_account(user_id=user) is True

    async with mem.factory() as session:
        assert await session.get(UserDataKey, mem.user) is None
        for model in (MemoryFact, MemoryPerson, MemoryAccessLog):
            count = await session.scalar(
                select(func.count()).select_from(model).where(model.user_id == mem.user)
            )
            assert count == 0
    # A new DEK (created on next use) cannot open the «backup» copy.
    async with mem.factory() as session, session.begin():
        fresh = await mem.vault.ensure_dek(session, mem.user)
    assert fresh != dek
    with pytest.raises(DecryptionError):
        open_sealed(fresh, backup, aad=aad)
    # The other user's memory is untouched.
    other = str(mem.users[1])
    await mem.service.create_fact(user_id=other, text="Чужое", category="other")
    assert len((await mem.service.list_facts(user_id=other)).items) == 1


async def test_forget_all_keeps_settings(mem: MemoryWorld) -> None:
    user = str(mem.user)
    await mem.service.update_settings(user_id=user, history_retention="30d")
    await mem.service.create_fact(user_id=user, text="Факт", category="other")
    await mem.service.create_person(user_id=user, data=PersonInput("Яна"))
    await mem.service.put_preference(user_id=user, key="reply.language", value="ru")
    assert await mem.service.forget_all(user_id=user) == 3
    snapshot = await mem.service.snapshot(user_id=user)
    assert (snapshot.facts, snapshot.people, snapshot.preferences) == ([], [], [])
    assert (await mem.service.settings(user_id=user)).history_retention == "30d"


async def test_preferences_round_trip(mem: MemoryWorld) -> None:
    user = str(mem.user)
    await mem.service.put_preference(user_id=user, key="voice.speech_rate", value=1.2)
    updated = await mem.service.put_preference(user_id=user, key="voice.speech_rate", value=1.5)
    listed = await mem.service.list_preferences(user_id=user)
    assert [(p.key, p.value) for p in listed] == [("voice.speech_rate", 1.5)]
    assert_mvp_valid(updated.contract(), "memory", "Preference")


async def test_unknown_user_id_is_harmless(mem: MemoryWorld) -> None:
    assert await mem.service.context(user_id="nope", query="x") == ()
    assert await mem.service.resolve_person(user_id=str(uuid.uuid4()), name="Яна") == []
