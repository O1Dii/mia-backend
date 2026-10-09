"""Envelope encryption primitives: master key -> per-user DEK -> ciphertext.

Layout:

* A *master keyring* (``MasterKeyring``) holds one or more 256-bit master keys
  identified by a short id. It comes from process configuration only; the
  active key wraps new DEKs, retired keys stay in the ring to unwrap old ones
  until every DEK is re-wrapped (``MasterKeyring.rewrap``).
* A *DEK* (data-encryption key, 256 bit) is generated per user. Only its wrapped
  form is persisted. Deleting the wrapped DEK is a *crypto-shred*: every
  ciphertext sealed under it becomes unrecoverable, including in backups of the
  ciphertext columns.
* ``seal``/``open_sealed`` use AES-256-GCM with a random 96-bit nonce and
  caller-supplied associated data (AAD) that binds a ciphertext to its owner
  and purpose, so a blob copied between rows or users fails authentication.

This module is database-free and never logs. Key material is excluded from
``repr`` and from every exception message.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
_BLOB_VERSION = b"\x01"
_MIN_BLOB = len(_BLOB_VERSION) + NONCE_BYTES + 16


class CryptoError(Exception):
    """Base class; messages never contain key material or plaintext."""


class MasterKeyConfigError(CryptoError):
    """The master keyring configuration is missing or malformed."""


class UnknownMasterKeyError(CryptoError):
    """A wrapped DEK references a master key id that is not in the keyring."""


class DecryptionError(CryptoError):
    """Authentication failed: wrong key, wrong AAD, or corrupted/forged blob."""


def new_dek() -> bytes:
    """Generate a fresh random 256-bit data-encryption key."""
    return os.urandom(KEY_BYTES)


def seal(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes:
    """Encrypt ``plaintext``; output is ``version || nonce || ciphertext+tag``."""
    _check_key(key)
    nonce = os.urandom(NONCE_BYTES)
    return _BLOB_VERSION + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def open_sealed(key: bytes, blob: bytes, *, aad: bytes) -> bytes:
    """Decrypt a blob produced by ``seal``; raises ``DecryptionError`` on any mismatch."""
    _check_key(key)
    if len(blob) < _MIN_BLOB or blob[:1] != _BLOB_VERSION:
        raise DecryptionError("Ciphertext is malformed or has an unsupported version.")
    nonce = blob[1 : 1 + NONCE_BYTES]
    try:
        return AESGCM(key).decrypt(nonce, blob[1 + NONCE_BYTES :], aad)
    except InvalidTag:
        raise DecryptionError("Ciphertext failed authentication.") from None


def _check_key(key: bytes) -> None:
    if len(key) != KEY_BYTES:
        raise CryptoError("Encryption keys must be exactly 32 bytes.")


class MasterKeyring:
    """Versioned master keys; the active one wraps new DEKs."""

    def __init__(self, keys: Mapping[str, bytes], active_id: str) -> None:
        if not keys:
            raise MasterKeyConfigError("At least one master key is required.")
        for key_id, key in keys.items():
            if not key_id or len(key) != KEY_BYTES:
                raise MasterKeyConfigError("Master keys must be 32 bytes with a non-empty id.")
        if active_id not in keys:
            raise MasterKeyConfigError("The active master key id is not in the keyring.")
        self._keys = dict(keys)
        self._active_id = active_id

    def __repr__(self) -> str:
        return f"MasterKeyring(ids={sorted(self._keys)!r}, active={self._active_id!r})"

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    @classmethod
    def from_config(cls, spec: str, active_id: str | None = None) -> MasterKeyring:
        """Parse ``"id1:<base64>,id2:<base64>"``; the last id is active unless given."""
        keys: dict[str, bytes] = {}
        last: str | None = None
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            key_id, separator, encoded = part.partition(":")
            key_id = key_id.strip()
            if not separator or not key_id or key_id in keys:
                raise MasterKeyConfigError(
                    "Master keys must look like 'id:base64' with unique ids."
                )
            keys[key_id] = _decode_key(encoded.strip())
            last = key_id
        if last is None:
            raise MasterKeyConfigError("At least one master key is required.")
        return cls(keys, active_id or last)

    def wrap(self, dek: bytes, *, context: bytes) -> tuple[str, bytes]:
        """Wrap ``dek`` under the active master key; returns ``(key_id, blob)``."""
        return self._active_id, seal(self._master(self._active_id), dek, aad=context)

    def unwrap(self, key_id: str, blob: bytes, *, context: bytes) -> bytes:
        return open_sealed(self._master(key_id), blob, aad=context)

    def rewrap(self, key_id: str, blob: bytes, *, context: bytes) -> tuple[str, bytes]:
        """Re-encrypt a wrapped DEK under the active key (master-key rotation)."""
        return self.wrap(self.unwrap(key_id, blob, context=context), context=context)

    def _master(self, key_id: str) -> bytes:
        try:
            return self._keys[key_id]
        except KeyError:
            raise UnknownMasterKeyError("The master key for this DEK is not configured.") from None


def _decode_key(encoded: str) -> bytes:
    try:
        normalized = encoded.replace("-", "+").replace("_", "/")
        key = base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise MasterKeyConfigError("A master key is not valid base64.") from None
    if len(key) != KEY_BYTES:
        raise MasterKeyConfigError("A master key must decode to exactly 32 bytes.")
    return key


def generate_master_key() -> str:
    """Return a new base64url master key for ``id:key`` configuration (operator tooling)."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode().rstrip("=")
