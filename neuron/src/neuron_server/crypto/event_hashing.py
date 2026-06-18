# SPDX-License-Identifier: Apache-2.0
"""Event content hashes, reference hashes, event IDs and event signing (HS-7).

Implements the spec's "Signing Events" / "Event IDs" rules for room version 11:

* the **content hash** commits to the full event and is stored in ``hashes``;
* the **reference hash** is the SHA-256 of the *redacted* event (minus
  ``signatures``/``unsigned``) — and the event's **ID** is ``$`` followed by the
  URL-safe unpadded base64 of that hash;
* an event is **signed** by signing its redacted form (which carries the content
  hash), so the signature transitively commits to the whole event.

Built from the Matrix spec; the Ed25519 maths is delegated to PyNaCl via
:mod:`neuron_server.crypto.signing`.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from neuron_server.crypto.signing import (
    SigningKey,
    canonical_json,
    decode_unpadded_base64,
    encode_unpadded_base64_urlsafe,
    sign_json,
    verify_signed_json,
)
from neuron_server.rooms import versions

# Top-level keys preserved by the redaction algorithm in room version 11
# (MSC2176 removed ``origin``, ``membership`` and ``prev_state``; ``redacts``
# lives inside ``content`` since MSC2174).
_TOP_LEVEL_KEEP = frozenset(
    {
        "event_id",
        "type",
        "room_id",
        "sender",
        "state_key",
        "content",
        "hashes",
        "signatures",
        "depth",
        "prev_events",
        "auth_events",
        "origin_server_ts",
    }
)

# Fields excluded when computing the content hash (everything the hash must not
# cover): the signatures, the unsigned annotations, and the hashes object itself.
_CONTENT_HASH_EXCLUDE = frozenset({"signatures", "unsigned", "hashes", "outlier", "destination"})


def _encode_standard_unpadded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def redact_event(
    pdu: dict[str, Any], room_version: str = versions.DEFAULT_ROOM_VERSION
) -> dict[str, Any]:
    """Return the redacted form of ``pdu`` per the room version's algorithm."""
    redacted = {key: pdu[key] for key in _TOP_LEVEL_KEEP if key in pdu}
    redacted["content"] = versions.redact_content(
        str(pdu.get("type", "")), dict(pdu.get("content") or {})
    )
    return redacted


def compute_content_hash(pdu: dict[str, Any]) -> str:
    """The unpadded-standard-base64 SHA-256 content hash stored in ``hashes``."""
    stripped = {k: v for k, v in pdu.items() if k not in _CONTENT_HASH_EXCLUDE}
    return _encode_standard_unpadded(hashlib.sha256(canonical_json(stripped)).digest())


def reference_hash(
    pdu: dict[str, Any], room_version: str = versions.DEFAULT_ROOM_VERSION
) -> bytes:
    """The SHA-256 reference hash: digest of the redacted event sans signatures."""
    redacted = redact_event(pdu, room_version)
    redacted.pop("signatures", None)
    redacted.pop("unsigned", None)
    return hashlib.sha256(canonical_json(redacted)).digest()


def compute_event_id(
    pdu: dict[str, Any], room_version: str = versions.DEFAULT_ROOM_VERSION
) -> str:
    """The room v4+ event ID: ``$`` + URL-safe unpadded base64 of the ref hash."""
    return "$" + encode_unpadded_base64_urlsafe(reference_hash(pdu, room_version))


def add_hashes_and_signatures(
    pdu: dict[str, Any],
    *,
    server_name: str,
    signing_key: SigningKey,
    room_version: str = versions.DEFAULT_ROOM_VERSION,
) -> dict[str, Any]:
    """Return ``pdu`` with its ``hashes`` and this server's ``signatures`` added."""
    result = dict(pdu)
    result["hashes"] = {"sha256": compute_content_hash(result)}
    redacted = redact_event(result, room_version)
    redacted.pop("unsigned", None)
    signed = sign_json(redacted, server_name=server_name, signing_key=signing_key)
    result["signatures"] = signed["signatures"]
    return result


def verify_event_signature(
    pdu: dict[str, Any],
    *,
    server_name: str,
    verify_key_base64: str,
    key_id: str,
    room_version: str = versions.DEFAULT_ROOM_VERSION,
) -> bool:
    """Verify ``server_name``'s signature over ``pdu`` (checks the redacted form)."""
    redacted = redact_event(pdu, room_version)
    redacted.pop("unsigned", None)
    redacted["signatures"] = pdu.get("signatures", {})
    return verify_signed_json(
        redacted, server_name=server_name, verify_key_base64=verify_key_base64, key_id=key_id
    )


def verify_content_hash(pdu: dict[str, Any]) -> bool:
    """Check that ``pdu``'s stored content hash matches its current content."""
    try:
        claimed = pdu["hashes"]["sha256"]
    except (KeyError, TypeError):
        return False
    # Compare against the raw bytes to tolerate padding differences.
    return decode_unpadded_base64(claimed) == decode_unpadded_base64(compute_content_hash(pdu))
