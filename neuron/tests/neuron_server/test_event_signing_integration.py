# SPDX-License-Identifier: Apache-2.0
"""End-to-end: events produced by RoomService are signed, reference-hashed PDUs.

Drives the real RoomService against a temp-file database and checks that every
stored event's ID equals its reference hash, that the server's signature
verifies, and that auth/prev event links are correct — i.e. the events would be
acceptable to a remote homeserver.
"""

from __future__ import annotations

from pathlib import Path

from neuron_server.crypto.event_hashing import compute_event_id, verify_event_signature
from neuron_server.crypto.signing import SigningKey, generate_signing_key
from neuron_server.rooms.service import RoomService
from neuron_server.storage import rooms as store
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.migrations import run_migrations

_SERVER = "neuron.local"
_ALICE = "@alice:neuron.local"


async def _setup(tmp_path: Path) -> tuple[Database, RoomService, SigningKey]:
    db = connect_database(f"sqlite:///{tmp_path / 'hs.db'}")
    await db.connect()
    await run_migrations(db)
    key = generate_signing_key("a_test")
    return db, RoomService(db, _SERVER, key), key


def _assert_well_formed(event, key: SigningKey) -> None:
    pdu = event.pdu_dict()
    assert event.event_id == compute_event_id(pdu), "event ID must be the reference hash"
    assert verify_event_signature(
        pdu, server_name=_SERVER, verify_key_base64=key.verify_key_base64(), key_id=key.key_id
    ), "server signature must verify"
    assert event.hashes and "sha256" in event.hashes


async def test_created_room_events_are_signed_pdus(tmp_path: Path) -> None:
    db, rooms, key = await _setup(tmp_path)
    try:
        room_id = await rooms.create_room(_ALICE, {"name": "Room"})

        create = await store.get_state_event(db, room_id, "m.room.create", "")
        member = await store.get_state_event(db, room_id, "m.room.member", _ALICE)
        power = await store.get_state_event(db, room_id, "m.room.power_levels", "")
        assert create is not None and member is not None and power is not None

        for event in (create, member, power):
            _assert_well_formed(event, key)

        # The create event roots the DAG; later events chain off it.
        assert create.auth_events == [] and create.prev_events == []
        assert create.event_id in member.auth_events
        assert member.prev_events == [create.event_id]
        # power_levels auths against create and the creator's membership.
        assert create.event_id in power.auth_events
        assert member.event_id in power.auth_events
    finally:
        await db.disconnect()


async def test_sent_message_is_signed_and_chained(tmp_path: Path) -> None:
    db, rooms, key = await _setup(tmp_path)
    try:
        room_id = await rooms.create_room(_ALICE, {})
        event_id = await rooms.send_message(
            room_id, _ALICE, "m.room.message", {"msgtype": "m.text", "body": "hi"}, "txn1"
        )
        event = await store.get_event(db, room_id, event_id)
        assert event is not None
        _assert_well_formed(event, key)
        assert len(event.prev_events) == 1  # chains onto the previous extremity
        # Auth events: create, power levels, and the sender's membership.
        assert len(event.auth_events) == 3
    finally:
        await db.disconnect()
