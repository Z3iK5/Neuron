# SPDX-License-Identifier: Apache-2.0
"""Validation of inbound federation PDUs (HS-7).

Before an event received from another server may be trusted, it must be checked:
the required fields are present and within size limits, its **content hash** is
intact, and it carries a valid **signature from the sender's server** (resolved
via the key resolver). This is the security-critical gate on the federation
ingress path; durable state application (auth against the event's ``auth_events``
and state resolution) is the next step.

Built from the Matrix spec ("Checking for a valid signature" / "Validating an
event").
"""

from __future__ import annotations

from typing import Any, Protocol

from neuron_server.crypto.event_hashing import (
    compute_event_id,
    verify_content_hash,
    verify_event_signature,
)
from neuron_server.crypto.signing import canonical_json
from neuron_server.rooms import versions

# The spec caps a PDU's canonical form at 65536 bytes.
_MAX_PDU_BYTES = 65536

_REQUIRED_FIELDS = (
    "type",
    "room_id",
    "sender",
    "content",
    "auth_events",
    "prev_events",
    "depth",
    "origin_server_ts",
    "hashes",
    "signatures",
)


class KeyResolver(Protocol):
    async def verify_keys_for(self, server_name: str) -> dict[str, str]: ...


class PduValidationError(Exception):
    """An inbound PDU failed validation; ``reason`` is safe to return to the peer."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def domain_of(user_or_server: str) -> str:
    return user_or_server.split(":", 1)[1] if ":" in user_or_server else user_or_server


def check_structure(pdu: Any) -> None:
    """Validate a PDU's shape and size (no cryptography)."""
    if not isinstance(pdu, dict):
        raise PduValidationError("PDU is not a JSON object")
    for field in _REQUIRED_FIELDS:
        if field not in pdu:
            raise PduValidationError(f"PDU missing required field {field!r}")
    if not isinstance(pdu["content"], dict):
        raise PduValidationError("PDU content must be an object")
    sender = pdu["sender"]
    if not isinstance(sender, str) or ":" not in sender:
        raise PduValidationError("PDU has an invalid sender")
    if len(canonical_json(pdu)) > _MAX_PDU_BYTES:
        raise PduValidationError("PDU exceeds the maximum event size")


async def validate_pdu(
    pdu: Any, *, resolver: KeyResolver, room_version: str = versions.DEFAULT_ROOM_VERSION
) -> str:
    """Validate an inbound PDU and return its (reference-hash) event ID.

    Raises :class:`PduValidationError` if the PDU is malformed, its content hash
    does not match, or it lacks a verifiable signature from the sender's server.
    """
    check_structure(pdu)
    event_id = compute_event_id(pdu, room_version)

    if not verify_content_hash(pdu):
        raise PduValidationError("content hash mismatch")

    sender_domain = domain_of(pdu["sender"])
    verify_keys = await resolver.verify_keys_for(sender_domain)
    if not verify_keys:
        raise PduValidationError(f"could not resolve signing keys for {sender_domain!r}")

    signatures = pdu.get("signatures", {})
    sender_sigs = signatures.get(sender_domain) if isinstance(signatures, dict) else None
    if not isinstance(sender_sigs, dict) or not sender_sigs:
        raise PduValidationError(f"PDU is not signed by the sender's server {sender_domain!r}")

    verified = any(
        key_id in verify_keys
        and verify_event_signature(
            pdu,
            server_name=sender_domain,
            verify_key_base64=verify_keys[key_id],
            key_id=key_id,
            room_version=room_version,
        )
        for key_id in sender_sigs
    )
    if not verified:
        raise PduValidationError("sender server signature did not verify")
    return event_id


def best_effort_event_id(pdu: Any) -> str:
    """An event ID for result-keying even when a PDU is malformed."""
    try:
        return compute_event_id(pdu)
    except Exception:
        return "unknown"
