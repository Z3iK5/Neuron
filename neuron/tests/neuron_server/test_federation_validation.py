# SPDX-License-Identifier: Apache-2.0
"""Unit tests for inbound federation PDU validation (HS-7 step 5)."""

from __future__ import annotations

import pytest

from neuron_server.crypto.event_hashing import add_hashes_and_signatures, compute_event_id
from neuron_server.crypto.signing import SigningKey, generate_signing_key
from neuron_server.federation.validation import PduValidationError, validate_pdu


class _FakeResolver:
    def __init__(self, mapping: dict[str, dict[str, str]]) -> None:
        self._mapping = mapping

    async def verify_keys_for(self, server_name: str) -> dict[str, str]:
        return self._mapping.get(server_name, {})


def _signed_pdu(key: SigningKey, *, sender: str = "@a:hs.a") -> dict:
    pdu = {
        "room_id": "!r:hs.a",
        "type": "m.room.message",
        "sender": sender,
        "content": {"msgtype": "m.text", "body": "hello"},
        "origin_server_ts": 1000,
        "depth": 5,
        "prev_events": ["$prev"],
        "auth_events": ["$create"],
    }
    return add_hashes_and_signatures(pdu, server_name="hs.a", signing_key=key)


def _resolver_for(key: SigningKey, domain: str = "hs.a") -> _FakeResolver:
    return _FakeResolver({domain: {key.key_id: key.verify_key_base64()}})


async def test_validate_accepts_well_formed_pdu() -> None:
    key = generate_signing_key("a_k")
    pdu = _signed_pdu(key)
    event_id = await validate_pdu(pdu, resolver=_resolver_for(key))
    assert event_id == compute_event_id(pdu)


async def test_validate_rejects_tampered_content() -> None:
    key = generate_signing_key("a_k")
    pdu = _signed_pdu(key)
    pdu["content"] = {"msgtype": "m.text", "body": "TAMPERED"}
    with pytest.raises(PduValidationError, match="content hash"):
        await validate_pdu(pdu, resolver=_resolver_for(key))


async def test_validate_rejects_bad_signature() -> None:
    key = generate_signing_key("a_k")
    pdu = _signed_pdu(key)
    # Re-point the signature at a different key the resolver still vouches for.
    other = generate_signing_key("a_k")
    with pytest.raises(PduValidationError, match="signature"):
        await validate_pdu(pdu, resolver=_resolver_for(other))


async def test_validate_rejects_unresolvable_sender() -> None:
    key = generate_signing_key("a_k")
    pdu = _signed_pdu(key)
    with pytest.raises(PduValidationError, match="resolve signing keys"):
        await validate_pdu(pdu, resolver=_FakeResolver({}))


async def test_validate_rejects_missing_fields() -> None:
    key = generate_signing_key("a_k")
    pdu = _signed_pdu(key)
    del pdu["auth_events"]
    with pytest.raises(PduValidationError, match="missing required field"):
        await validate_pdu(pdu, resolver=_resolver_for(key))


async def test_validate_requires_sender_server_signature() -> None:
    key = generate_signing_key("a_k")
    # Signed by hs.a but the sender lives on hs.b → no signature from hs.b.
    pdu = _signed_pdu(key, sender="@a:hs.b")
    resolver = _FakeResolver({"hs.b": {key.key_id: key.verify_key_base64()}})
    with pytest.raises(PduValidationError, match="not signed by the sender"):
        await validate_pdu(pdu, resolver=resolver)
