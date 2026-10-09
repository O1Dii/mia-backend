from __future__ import annotations

import pytest
from app.core.crypto import (
    CryptoError,
    DecryptionError,
    MasterKeyConfigError,
    MasterKeyring,
    UnknownMasterKeyError,
    generate_master_key,
    new_dek,
    open_sealed,
    seal,
)


def test_seal_round_trip_and_fresh_nonce() -> None:
    key = new_dek()
    first = seal(key, b"secret", aad=b"a")
    second = seal(key, b"secret", aad=b"a")
    assert first != second
    assert open_sealed(key, first, aad=b"a") == b"secret"
    assert b"secret" not in first


@pytest.mark.parametrize("mutate", ["aad", "key", "tamper", "short", "version"])
def test_open_rejects_wrong_context_key_or_tampering(mutate: str) -> None:
    key = new_dek()
    blob = seal(key, b"secret", aad=b"a")
    aad = b"a"
    if mutate == "aad":
        aad = b"b"
    elif mutate == "key":
        key = new_dek()
    elif mutate == "tamper":
        blob = blob[:-1] + bytes([blob[-1] ^ 1])
    elif mutate == "short":
        blob = blob[:10]
    else:
        blob = b"\x02" + blob[1:]
    with pytest.raises(DecryptionError):
        open_sealed(key, blob, aad=aad)


def test_keys_must_be_32_bytes() -> None:
    with pytest.raises(CryptoError):
        seal(b"short", b"x", aad=b"")


def test_keyring_parses_config_and_hides_key_material() -> None:
    first, second = generate_master_key(), generate_master_key()
    ring = MasterKeyring.from_config(f"old:{first}, new:{second}")
    assert ring.active_id == "new"
    assert ring.key_ids == {"old", "new"}
    assert first not in repr(ring) and second not in repr(ring)
    assert MasterKeyring.from_config(f"old:{first},new:{second}", "old").active_id == "old"


@pytest.mark.parametrize(
    "spec",
    ["", "noseparator", "a:!!!notbase64", "a:AAAA", "a:" + "A" * 43 + ",a:" + "A" * 43],
)
def test_keyring_rejects_bad_config(spec: str) -> None:
    with pytest.raises(MasterKeyConfigError):
        MasterKeyring.from_config(spec)


def test_keyring_rejects_unknown_active_id() -> None:
    with pytest.raises(MasterKeyConfigError):
        MasterKeyring.from_config(f"a:{generate_master_key()}", "missing")


def test_master_key_rotation_rewraps_and_retired_key_can_be_dropped() -> None:
    old_key, new_key = generate_master_key(), generate_master_key()
    old_ring = MasterKeyring.from_config(f"k1:{old_key}")
    dek = new_dek()
    key_id, blob = old_ring.wrap(dek, context=b"user-1")
    assert key_id == "k1"

    both = MasterKeyring.from_config(f"k1:{old_key},k2:{new_key}")
    assert both.unwrap(key_id, blob, context=b"user-1") == dek
    new_id, new_blob = both.rewrap(key_id, blob, context=b"user-1")
    assert new_id == "k2"

    only_new = MasterKeyring.from_config(f"k2:{new_key}")
    assert only_new.unwrap(new_id, new_blob, context=b"user-1") == dek
    with pytest.raises(UnknownMasterKeyError):
        only_new.unwrap(key_id, blob, context=b"user-1")
    with pytest.raises(DecryptionError):
        only_new.unwrap(new_id, new_blob, context=b"user-2")
