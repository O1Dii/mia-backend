"""Envelope encryption against PostgreSQL: per-user DEKs, master rotation, crypto-shred."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
from app.core.crypto import DecryptionError, MasterKeyring, generate_master_key
from app.modules.models.connections import ConnectionService
from app.modules.models.models import ModelConnection, UserDataKey
from app.modules.models.vault import DekUnavailableError, KeyVault
from sqlalchemy import select, text

from tests.models.conftest import SessionFactory

MakeUser = Callable[[], Awaitable[uuid.UUID]]
SECRET = "sk-live-VERY-SECRET-0123456789"


async def test_each_user_gets_an_independent_dek_and_secrets_are_bound_to_their_record(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    vault = KeyVault(keyring)
    alice, bob = await make_user(), await make_user()
    record = uuid.uuid4()
    async with session_factory() as session, session.begin():
        sealed = await vault.seal_secret(
            session, alice, SECRET.encode(), purpose="api_key", record_id=record
        )
        await vault.ensure_dek(session, bob)
    assert SECRET.encode() not in sealed

    async with session_factory() as session:
        keys = (await session.scalars(select(UserDataKey))).all()
        assert {k.user_id for k in keys} == {alice, bob}
        assert keys[0].wrapped_dek != keys[1].wrapped_dek
        assert (
            await vault.open_secret(session, alice, sealed, purpose="api_key", record_id=record)
            == SECRET.encode()
        )
        wrong_contexts: list[tuple[uuid.UUID, str, uuid.UUID]] = [
            (bob, "api_key", record),  # another user's DEK
            (alice, "plan_tokens", record),  # another purpose
            (alice, "api_key", uuid.uuid4()),  # another row
        ]
        for wrong_user, purpose, record_id in wrong_contexts:
            with pytest.raises(DecryptionError):
                await vault.open_secret(
                    session, wrong_user, sealed, purpose=purpose, record_id=record_id
                )


async def test_dek_creation_is_race_safe(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    import asyncio

    vault = KeyVault(keyring)
    user = await make_user()

    async def one() -> bytes:
        async with session_factory() as session, session.begin():
            return await vault.ensure_dek(session, user)

    deks = await asyncio.gather(*(one() for _ in range(8)))
    assert len(set(deks)) == 1


async def test_master_key_rotation_rewraps_deks_without_touching_ciphertexts(
    session_factory: SessionFactory, make_user: MakeUser
) -> None:
    k1, k2 = generate_master_key(), generate_master_key()
    old_vault = KeyVault(MasterKeyring.from_config(f"k1:{k1}"))
    user = await make_user()
    record = uuid.uuid4()
    async with session_factory() as session, session.begin():
        sealed = await old_vault.seal_secret(
            session, user, b"payload", purpose="api_key", record_id=record
        )

    rotated = KeyVault(MasterKeyring.from_config(f"k1:{k1},k2:{k2}"))
    async with session_factory() as session, session.begin():
        assert await rotated.rotate_master_key(session) == 1
    async with session_factory() as session, session.begin():
        assert await rotated.rotate_master_key(session) == 0  # idempotent
        row = await session.get(UserDataKey, user)
        assert row is not None and row.master_key_id == "k2" and row.rewrapped_at is not None

    only_new = KeyVault(MasterKeyring.from_config(f"k2:{k2}"))  # k1 retired and deleted
    async with session_factory() as session:
        assert (
            await only_new.open_secret(session, user, sealed, purpose="api_key", record_id=record)
            == b"payload"
        )


async def test_crypto_shred_makes_connection_secrets_unrecoverable(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    vault = KeyVault(keyring)
    service = ConnectionService(session_factory, vault)
    user, bystander = await make_user(), await make_user()
    mine = await service.create(
        user, provider="deepinfra", label="DI", model="zai-org/GLM-5.3-Flash", api_key=SECRET
    )
    theirs = await service.create(
        bystander, provider="deepinfra", label="DI", model="m", api_key="bystander-key-0123456789"
    )

    async with session_factory() as session:  # a "backup" of the ciphertext column
        backup = await session.scalar(
            select(ModelConnection.secret_ciphertext).where(ModelConnection.id == mine.id)
        )
    assert backup is not None and SECRET.encode() not in backup
    assert (await service.resolve(user, "deepinfra/zai-org/GLM-5.3-Flash"))[0].api_key is not None

    async with session_factory() as session, session.begin():
        assert await vault.shred_user(session, user) is True
        assert await vault.shred_user(session, user) is False

    async with session_factory() as session:
        assert await session.get(UserDataKey, user) is None
        with pytest.raises(DekUnavailableError):
            await vault.open_secret(session, user, backup, purpose="api_key", record_id=mine.id)
        # even a brand-new DEK for the same user cannot read the old blob
    async with session_factory() as session, session.begin():
        fresh = await vault.ensure_dek(session, user)
        assert fresh
    async with session_factory() as session:
        with pytest.raises(DecryptionError):
            await vault.open_secret(session, user, backup, purpose="api_key", record_id=mine.id)

    after = await service.get(user, mine.id)
    assert (after.has_key, after.key_last4, after.status) == (False, None, "needs_reauth")
    assert await service.resolve(user, "mia:default") == []
    # other users are untouched
    other = await service.resolve(bystander, "mia:default")
    assert other and other[0].api_key is not None and theirs.has_key


async def test_user_deletion_cascades_the_dek_and_connections(
    session_factory: SessionFactory, keyring: MasterKeyring, make_user: MakeUser
) -> None:
    service = ConnectionService(session_factory, KeyVault(keyring))
    user = await make_user()
    await service.create(user, provider="groq", label="g", model="m", api_key=SECRET)
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user})
    async with session_factory() as session:
        assert await session.scalar(select(UserDataKey).where(UserDataKey.user_id == user)) is None
        assert (await session.scalars(select(ModelConnection))).all() == []
