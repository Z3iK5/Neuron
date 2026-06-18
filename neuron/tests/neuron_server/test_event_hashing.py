# SPDX-License-Identifier: Apache-2.0
"""Unit tests for event hashing, reference-hash event IDs and event signing."""

from __future__ import annotations

from neuron_server.crypto.event_hashing import (
    add_hashes_and_signatures,
    compute_event_id,
    redact_event,
    verify_content_hash,
    verify_event_signature,
)
from neuron_server.crypto.signing import decode_unpadded_base64, generate_signing_key
from neuron_server.rooms.authrules import select_auth_event_ids
from neuron_server.rooms.events import Event


def _pdu(**overrides: object) -> dict:
    pdu: dict = {
        "room_id": "!r:hs",
        "type": "m.room.message",
        "sender": "@a:hs",
        "content": {"body": "hi", "msgtype": "m.text"},
        "origin_server_ts": 1000,
        "depth": 5,
        "prev_events": ["$prev"],
        "auth_events": ["$create", "$pl"],
    }
    pdu.update(overrides)
    return pdu


def test_redact_event_keeps_allowlisted_top_level_and_strips_content() -> None:
    redacted = redact_event(_pdu(unsigned={"age": 1}, origin="hs"))
    assert "origin" not in redacted  # removed in v11
    assert "unsigned" not in redacted
    assert redacted["content"] == {}  # m.room.message keeps no content keys
    assert redacted["sender"] == "@a:hs"
    assert redacted["auth_events"] == ["$create", "$pl"]


def test_redact_event_member_keeps_membership_only() -> None:
    pdu = _pdu(
        type="m.room.member",
        state_key="@b:hs",
        content={"membership": "join", "displayname": "B", "avatar_url": "x"},
    )
    assert redact_event(pdu)["content"] == {"membership": "join"}


def test_redact_event_create_keeps_all_content() -> None:
    pdu = _pdu(type="m.room.create", state_key="", content={"room_version": "11", "x": 1})
    assert redact_event(pdu)["content"] == {"room_version": "11", "x": 1}


def test_event_id_is_dollar_plus_32_byte_urlsafe_hash() -> None:
    event_id = compute_event_id(_pdu())
    assert event_id.startswith("$")
    assert "/" not in event_id and "+" not in event_id  # URL-safe alphabet
    assert len(decode_unpadded_base64(event_id[1:].replace("-", "+").replace("_", "/"))) == 32


def test_event_id_changes_with_content() -> None:
    # The content hash (kept by redaction) is what ties the event ID to content,
    # so compare fully hashed events.
    key = generate_signing_key("a_k")
    one = add_hashes_and_signatures(_pdu(), server_name="hs", signing_key=key)
    two = add_hashes_and_signatures(
        _pdu(content={"body": "different", "msgtype": "m.text"}), server_name="hs", signing_key=key
    )
    assert compute_event_id(one) != compute_event_id(two)


def test_sign_event_then_verify_and_tamper() -> None:
    key = generate_signing_key("a_k")
    signed = add_hashes_and_signatures(_pdu(), server_name="hs", signing_key=key)
    assert "sha256" in signed["hashes"]
    assert verify_content_hash(signed)
    assert verify_event_signature(
        signed, server_name="hs", verify_key_base64=key.verify_key_base64(), key_id=key.key_id
    )
    # Tampering with content breaks the content hash...
    tampered = dict(signed)
    tampered["content"] = {"body": "evil", "msgtype": "m.text"}
    assert not verify_content_hash(tampered)
    # ...and a tampered auth_events list breaks the signature.
    tampered2 = dict(signed)
    tampered2["auth_events"] = ["$evil"]
    assert not verify_event_signature(
        tampered2, server_name="hs", verify_key_base64=key.verify_key_base64(), key_id=key.key_id
    )


def test_signature_ignores_unsigned() -> None:
    key = generate_signing_key("a_k")
    signed = add_hashes_and_signatures(_pdu(), server_name="hs", signing_key=key)
    signed["unsigned"] = {"redacted_because": {"x": 1}}  # added after signing
    assert verify_event_signature(
        signed, server_name="hs", verify_key_base64=key.verify_key_base64(), key_id=key.key_id
    )


def _member(event_id: str, user: str) -> Event:
    return Event(
        event_id=event_id, room_id="!r:hs", type="m.room.member", sender=user,
        content={"membership": "join"}, origin_server_ts=0, depth=1, stream_ordering=0,
        state_key=user,
    )


def test_select_auth_event_ids() -> None:
    create = Event(
        event_id="$c", room_id="!r:hs", type="m.room.create", sender="@a:hs",
        content={}, origin_server_ts=0, depth=1, stream_ordering=0, state_key="",
    )
    pl = Event(
        event_id="$pl", room_id="!r:hs", type="m.room.power_levels", sender="@a:hs",
        content={}, origin_server_ts=0, depth=1, stream_ordering=0, state_key="",
    )
    join_rules = Event(
        event_id="$jr", room_id="!r:hs", type="m.room.join_rules", sender="@a:hs",
        content={}, origin_server_ts=0, depth=1, stream_ordering=0, state_key="",
    )
    alice = _member("$ma", "@a:hs")
    state = {
        ("m.room.create", ""): create,
        ("m.room.power_levels", ""): pl,
        ("m.room.join_rules", ""): join_rules,
        ("m.room.member", "@a:hs"): alice,
    }
    # The create event is its own auth root.
    assert select_auth_event_ids("m.room.create", "", "@a:hs", {}, state) == []
    # A message auths against create, power levels and the sender's membership.
    assert select_auth_event_ids("m.room.message", None, "@a:hs", {}, state) == ["$c", "$pl", "$ma"]
    # A join additionally pulls in join rules and the target's membership.
    join_auth = select_auth_event_ids(
        "m.room.member", "@a:hs", "@a:hs", {"membership": "join"}, state
    )
    assert set(join_auth) == {"$c", "$pl", "$ma", "$jr"}
