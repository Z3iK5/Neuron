# SPDX-License-Identifier: Apache-2.0
"""Ed25519 server signing keys and Matrix JSON signing (HS-7, federation).

Implements the cryptographic primitives the spec requires for server-to-server
identity: canonical JSON, unpadded base64, Ed25519 key generation/serialisation
(Synapse-compatible ``ed25519 <key_id> <base64-seed>`` file format), and the
``signatures`` envelope used by ``sign_json`` / ``verify_signed_json``.

Built from the Matrix spec ("Signing JSON", "Canonical JSON") and RFC 8032; the
Ed25519 maths is delegated to libsodium via PyNaCl.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from nacl.signing import SigningKey as _NaclSigningKey
from nacl.signing import VerifyKey as _NaclVerifyKey

# --- base64 / canonical JSON ----------------------------------------------


def encode_unpadded_base64(data: bytes) -> str:
    """Standard base64 with ``=`` padding stripped (Matrix "unpadded base64")."""
    return base64.b64encode(data).decode("ascii").rstrip("=")


def decode_unpadded_base64(value: str) -> bytes:
    """Inverse of :func:`encode_unpadded_base64`; tolerant of missing padding."""
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding)


def encode_unpadded_base64_urlsafe(data: bytes) -> str:
    """URL-safe base64 with padding stripped (event IDs in room version 4+)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def canonical_json(value: Any) -> bytes:
    """Encode ``value`` as Matrix canonical JSON.

    Keys sorted lexicographically, no insignificant whitespace, UTF-8, and the
    ``signatures``/``unsigned`` keys are *not* special-cased here (callers strip
    them before signing). We reject non-finite floats to stay within the spec's
    integer-only number model for the data we sign.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


# --- signing keys ----------------------------------------------------------


@dataclass(frozen=True)
class SigningKey:
    """An Ed25519 signing key with its Matrix key id (e.g. ``ed25519:a_abcd``)."""

    version: str
    _signing: _NaclSigningKey

    @property
    def key_id(self) -> str:
        return f"ed25519:{self.version}"

    @property
    def seed(self) -> bytes:
        return bytes(self._signing)

    def verify_key_base64(self) -> str:
        return encode_unpadded_base64(bytes(self._signing.verify_key))

    def sign(self, message: bytes) -> bytes:
        return self._signing.sign(message).signature

    def serialize(self) -> str:
        """Synapse-compatible signing-key line: ``ed25519 <version> <b64-seed>``."""
        return f"ed25519 {self.version} {encode_unpadded_base64(self.seed)}"


def generate_signing_key(version: str) -> SigningKey:
    """Create a fresh random Ed25519 signing key with the given key version."""
    return SigningKey(version=version, _signing=_NaclSigningKey.generate())


def parse_signing_key(serialized: str) -> SigningKey:
    """Parse a Synapse-compatible ``ed25519 <version> <b64-seed>`` line."""
    parts = serialized.strip().split()
    if len(parts) != 3 or parts[0] != "ed25519":
        raise ValueError("Malformed signing key (expected 'ed25519 <version> <seed>')")
    seed = decode_unpadded_base64(parts[2])
    return SigningKey(version=parts[1], _signing=_NaclSigningKey(seed))


# --- JSON signatures -------------------------------------------------------


def signature_base(value: dict[str, Any]) -> bytes:
    """Canonical JSON of ``value`` with ``signatures`` and ``unsigned`` removed."""
    stripped = {k: v for k, v in value.items() if k not in ("signatures", "unsigned")}
    return canonical_json(stripped)


def sign_json(
    value: dict[str, Any], *, server_name: str, signing_key: SigningKey
) -> dict[str, Any]:
    """Return ``value`` with ``server_name``'s Ed25519 signature added.

    Follows the spec: sign the canonical JSON of the object minus its
    ``signatures`` and ``unsigned`` members, then merge the signature in under
    ``signatures[server_name][key_id]``.
    """
    signature = encode_unpadded_base64(signing_key.sign(signature_base(value)))
    signed = dict(value)
    signatures = {k: dict(v) for k, v in signed.get("signatures", {}).items()}
    signatures.setdefault(server_name, {})[signing_key.key_id] = signature
    signed["signatures"] = signatures
    return signed


def verify_signed_json(
    value: dict[str, Any], *, server_name: str, verify_key_base64: str, key_id: str
) -> bool:
    """Verify ``server_name``'s ``key_id`` signature over ``value``."""
    try:
        signature_b64 = value["signatures"][server_name][key_id]
    except (KeyError, TypeError):
        return False
    verify_key = _NaclVerifyKey(decode_unpadded_base64(verify_key_base64))
    try:
        verify_key.verify(signature_base(value), decode_unpadded_base64(signature_b64))
    except Exception:
        return False
    return True
