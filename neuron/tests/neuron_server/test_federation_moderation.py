# SPDX-License-Identifier: Apache-2.0
"""Federated moderation propagation (HS-7 follow-up).

Moderation, membership and redaction actions taken on a room a server *hosts*
must be pushed to every other server with a member in that room — otherwise a
kick/ban/leave/redaction is invisible across federation. These tests drive the
actions through the real Client-Server / Admin surface on one server and assert
the other server's copy of the room reflects them.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.crypto.event_hashing import compute_event_id
from neuron_server.federation.sender import FederationSender
from neuron_server.storage import rooms as store
from neuron_server.storage.database import Database

_CS = "/_matrix/client/v3"


def _opener(target_app: FastAPI) -> Callable[[str], httpx.AsyncClient]:
    def open_client(server_name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target_app), base_url=f"https://{server_name}"
        )

    return open_client


def _broken_opener(server_name: str) -> httpx.AsyncClient:
    raise ConnectionError(f"{server_name} is unreachable")


async def _register(client: httpx.AsyncClient, username: str) -> str:
    session = (
        await client.post(
            f"{_CS}/register", json={"username": username, "password": "pw-123456"}
        )
    ).json()["session"]
    out = await client.post(
        f"{_CS}/register",
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    )
    return out.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _membership(db: Database, room_id: str, user_id: str) -> str | None:
    event = await store.get_state_event(db, room_id, "m.room.member", user_id)
    return event.content.get("membership") if event is not None else None


async def _outbox_count(db: Database, destination: str) -> int:
    """Rows queued for a destination (test-only peek at the outbox table)."""
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM federation_outbox WHERE destination = ?", (destination,)
        )
    )


@dataclass
class _Net:
    app_a: FastAPI
    app_b: FastAPI
    client_a: httpx.AsyncClient
    client_b: httpx.AsyncClient
    db_a: Database
    db_b: Database


@contextlib.asynccontextmanager
async def _servers(tmp_path: Path, *, state_res_v2: bool = False) -> AsyncIterator[_Net]:
    app_a = create_app(
        NeuronServerSettings(
            name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}", state_res_v2=state_res_v2
        )
    )
    app_b = create_app(
        NeuronServerSettings(
            name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}", state_res_v2=state_res_v2
        )
    )
    async with app_b.router.lifespan_context(app_b), app_a.router.lifespan_context(app_a):
        app_a.state.federation_client.open_client = _opener(app_b)
        app_b.state.federation_client.open_client = _opener(app_a)
        client_a = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        )
        client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
        )
        try:
            yield _Net(app_a, app_b, client_a, client_b, app_a.state.db, app_b.state.db)
        finally:
            await client_a.aclose()
            await client_b.aclose()


async def _public_room(client: httpx.AsyncClient, token: str) -> str:
    return (
        await client.post(
            f"{_CS}/createRoom", headers=_auth(token), json={"preset": "public_chat"}
        )
    ).json()["room_id"]


async def _join_remote(client: httpx.AsyncClient, token: str, room_id: str, via: str) -> None:
    resp = await client.post(
        f"{_CS}/rooms/{room_id}/join", params={"server_name": via}, headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text


async def _apply_raw(
    app: FastAPI,
    *,
    room_id: str,
    etype: str,
    sender: str,
    content: dict,
    depth: int,
    state_key: str | None = None,
) -> tuple[str, bool]:
    """Feed a hand-built (unsigned) PDU straight into a server's apply path.

    apply_remote_event authorises against current state but does not re-verify
    signatures (that happens at the federation ingress endpoint), so an unsigned
    PDU is enough to exercise ordering/authority in isolation. Returns the event id
    and whether it was stored.
    """
    pdu: dict = {
        "room_id": room_id,
        "type": etype,
        "sender": sender,
        "content": content,
        "origin_server_ts": depth,
        "depth": depth,
        "prev_events": [],
        "auth_events": [],
    }
    if state_key is not None:
        pdu["state_key"] = state_key
    event_id = compute_event_id(pdu)
    stored = await app.state.rooms.apply_remote_event(pdu)
    return event_id, stored


# --- membership ------------------------------------------------------------


async def test_kick_propagates_to_remote_member(tmp_path: Path) -> None:
    """A resident kick of a remote member reaches the kicked user's own server,
    even though that server drops out of the joined-member set after the kick."""
    async with _servers(tmp_path) as net:
        bob = await _register(net.client_b, "bob")  # resident creator on B
        room = await _public_room(net.client_b, bob)
        alice = await _register(net.client_a, "alice")
        await _join_remote(net.client_a, alice, room, "b.test")
        assert "@alice:a.test" in await store.get_joined_members(net.db_a, room)

        r = await net.client_b.post(
            f"{_CS}/rooms/{room}/kick", headers=_auth(bob), json={"user_id": "@alice:a.test"}
        )
        assert r.status_code == 200, r.text

        # A's copy reflects the kick pushed from B.
        assert "@alice:a.test" not in await store.get_joined_members(net.db_a, room)
        assert await _membership(net.db_a, room, "@alice:a.test") == "leave"


async def test_ban_propagates_to_remote_member(tmp_path: Path) -> None:
    """A ban (not just a leave) propagates and lands as membership=ban remotely."""
    async with _servers(tmp_path) as net:
        bob = await _register(net.client_b, "bob")
        room = await _public_room(net.client_b, bob)
        alice = await _register(net.client_a, "alice")
        await _join_remote(net.client_a, alice, room, "b.test")

        r = await net.client_b.post(
            f"{_CS}/rooms/{room}/ban", headers=_auth(bob), json={"user_id": "@alice:a.test"}
        )
        assert r.status_code == 200, r.text

        assert await _membership(net.db_a, room, "@alice:a.test") == "ban"
        assert "@alice:a.test" not in await store.get_joined_members(net.db_a, room)


async def test_local_leave_propagates_to_remote_member(tmp_path: Path) -> None:
    """A local user leaving a resident room is seen by a remote co-member's server."""
    async with _servers(tmp_path) as net:
        alice = await _register(net.client_a, "alice")  # resident creator on A
        room = await _public_room(net.client_a, alice)
        bob = await _register(net.client_b, "bob")
        await _join_remote(net.client_b, bob, room, "a.test")

        carol = await _register(net.client_a, "carol")  # second local member on A
        join = await net.client_a.post(f"{_CS}/rooms/{room}/join", headers=_auth(carol))
        assert join.status_code == 200, join.text
        # The join propagated to B too.
        assert "@carol:a.test" in await store.get_joined_members(net.db_b, room)

        left = await net.client_a.post(f"{_CS}/rooms/{room}/leave", headers=_auth(carol))
        assert left.status_code == 200, left.text
        assert "@carol:a.test" not in await store.get_joined_members(net.db_b, room)


async def test_invite_propagates_to_in_room_remote_server(tmp_path: Path) -> None:
    """Inviting a local user is pushed to a remote member already in the room."""
    async with _servers(tmp_path) as net:
        alice = await _register(net.client_a, "alice")  # resident creator on A
        room = await _public_room(net.client_a, alice)
        bob = await _register(net.client_b, "bob")
        await _join_remote(net.client_b, bob, room, "a.test")

        r = await net.client_a.post(
            f"{_CS}/rooms/{room}/invite", headers=_auth(alice), json={"user_id": "@carol:a.test"}
        )
        assert r.status_code == 200, r.text
        # B (a member via Bob) receives the invite membership event.
        assert await _membership(net.db_b, room, "@carol:a.test") == "invite"


# --- redaction -------------------------------------------------------------


async def test_client_redaction_propagates(tmp_path: Path) -> None:
    """A user redacting their own message has the redaction applied on the
    server hosting the room, not just stored."""
    async with _servers(tmp_path) as net:
        alice = await _register(net.client_a, "alice")  # resident on A
        room = await _public_room(net.client_a, alice)
        bob = await _register(net.client_b, "bob")
        await _join_remote(net.client_b, bob, room, "a.test")

        sent = await net.client_b.put(
            f"{_CS}/rooms/{room}/send/m.room.message/m1",
            headers=_auth(bob),
            json={"msgtype": "m.text", "body": "redact me"},
        )
        event_id = sent.json()["event_id"]
        # The message federated to A.
        on_a = await store.get_event(net.db_a, room, event_id)
        assert on_a is not None and on_a.content.get("body") == "redact me"

        red = await net.client_b.put(
            f"{_CS}/rooms/{room}/redact/{event_id}/r1", headers=_auth(bob), json={}
        )
        assert red.status_code == 200, red.text

        # Both copies are actually scrubbed.
        for db in (net.db_a, net.db_b):
            target = await store.get_event(db, room, event_id)
            assert target is not None and target.content == {}


async def test_admin_redact_user_events_propagates(tmp_path: Path) -> None:
    """A server-authority bulk redaction of a remote user's messages reaches the
    remote user's own server."""
    async with _servers(tmp_path) as net:
        alice = await _register(net.client_a, "alice")  # resident on A
        room = await _public_room(net.client_a, alice)
        bob = await _register(net.client_b, "bob")
        await _join_remote(net.client_b, bob, room, "a.test")

        sent = await net.client_b.put(
            f"{_CS}/rooms/{room}/send/m.room.message/m1",
            headers=_auth(bob),
            json={"msgtype": "m.text", "body": "spam"},
        )
        event_id = sent.json()["event_id"]

        # Admin authority on A bulk-redacts Bob's events (called in-process).
        await net.app_a.state.admin.redact_user_events("@bob:b.test")

        for db in (net.db_a, net.db_b):
            target = await store.get_event(db, room, event_id)
            assert target is not None and target.content == {}


# --- room deletion ---------------------------------------------------------


async def test_admin_delete_room_kicks_remote_member(tmp_path: Path) -> None:
    """Deleting a resident room emits a creator-signed kick for each remote member
    that their server accepts and applies (a self-leave would be rejected)."""
    async with _servers(tmp_path) as net:
        bob = await _register(net.client_a, "bob")  # local creator on A
        room = await _public_room(net.client_a, bob)
        carol = await _register(net.client_b, "carol")
        await _join_remote(net.client_b, carol, room, "a.test")
        assert "@carol:b.test" in await store.get_joined_members(net.db_b, room)

        result = await net.app_a.state.admin.delete_room(room)
        assert "@carol:b.test" in result["kicked_users"]

        # B applied the kick: Carol is no longer joined on her own server.
        assert "@carol:b.test" not in await store.get_joined_members(net.db_b, room)
        assert await _membership(net.db_b, room, "@carol:b.test") == "leave"


# --- failure handling ------------------------------------------------------


async def test_moderation_is_best_effort_then_retried(tmp_path: Path) -> None:
    """A ban while the remote server is unreachable still succeeds locally, is
    queued, and is delivered on a later retry."""
    async with _servers(tmp_path) as net:
        alice = await _register(net.client_a, "alice")  # resident on A
        room = await _public_room(net.client_a, alice)
        carol = await _register(net.client_b, "carol")
        await _join_remote(net.client_b, carol, room, "a.test")

        # B goes offline; the ban can't be delivered.
        net.app_a.state.federation_client.open_client = _broken_opener
        r = await net.client_a.post(
            f"{_CS}/rooms/{room}/ban", headers=_auth(alice), json={"user_id": "@carol:b.test"}
        )
        assert r.status_code == 200, r.text  # local action unaffected
        assert await _membership(net.db_a, room, "@carol:b.test") == "ban"
        assert await _outbox_count(net.db_a, "b.test") > 0  # queued

        # B comes back; retry flushes the ban to it.
        net.app_a.state.federation_client.open_client = _opener(net.app_b)
        await net.app_a.state.federation_sender.retry("b.test")

        assert await _membership(net.db_b, room, "@carol:b.test") == "ban"
        assert await _outbox_count(net.db_a, "b.test") == 0  # drained


# --- sender unit -----------------------------------------------------------


async def test_send_transaction_unions_extra_destinations() -> None:
    """extra_destinations is unioned with the room's current members and our own
    server is never a destination."""
    sender = FederationSender(db=None, server_name="self.test", client=None)  # type: ignore[arg-type]
    recorded: list[str] = []

    async def fake_remote(room_id: str) -> set[str]:
        return {"x.test"}

    async def fake_deliver(server: str, *, new_pdus: list, edus: list) -> None:
        recorded.append(server)

    sender.remote_destinations = fake_remote  # type: ignore[method-assign]
    sender._deliver = fake_deliver  # type: ignore[method-assign]

    await sender._send_transaction(
        "!r:self.test", pdus=[{"k": "v"}], edus=[], extra_destinations={"y.test", "self.test"}
    )
    # x.test (current member) + y.test (extra); self.test filtered out; no dupes.
    assert set(recorded) == {"x.test", "y.test"}
    assert "self.test" not in recorded


async def test_redaction_reaches_already_banned_members_server(tmp_path: Path) -> None:
    """Ban a remote spammer, THEN redact their backlog: the redaction must still
    reach their server even though it has dropped out of the joined-member set."""
    async with _servers(tmp_path) as net:
        alice = await _register(net.client_a, "alice")  # resident creator on A
        room = await _public_room(net.client_a, alice)
        bob = await _register(net.client_b, "bob")
        await _join_remote(net.client_b, bob, room, "a.test")

        sent = await net.client_b.put(
            f"{_CS}/rooms/{room}/send/m.room.message/m1",
            headers=_auth(bob),
            json={"msgtype": "m.text", "body": "spam"},
        )
        event_id = sent.json()["event_id"]

        # Ban Bob — b.test is now no longer a joined-member server on A.
        ban = await net.client_a.post(
            f"{_CS}/rooms/{room}/ban", headers=_auth(alice), json={"user_id": "@bob:b.test"}
        )
        assert ban.status_code == 200, ban.text
        assert "@bob:b.test" not in await store.get_joined_members(net.db_a, room)

        # Now redact Bob's backlog; the redaction must still federate to b.test.
        await net.app_a.state.admin.redact_user_events("@bob:b.test")

        for db in (net.db_a, net.db_b):
            target = await store.get_event(db, room, event_id)
            assert target is not None and target.content == {}


async def test_out_of_order_redaction_is_reconciled(tmp_path: Path) -> None:
    """A redaction delivered before its target still scrubs the target once it
    arrives, and re-delivery is idempotent."""
    async with _servers(tmp_path) as net:
        alice = await _register(net.client_a, "alice")
        room = await _public_room(net.client_a, alice)

        # A remote user joins (applied directly), then we receive their redaction
        # BEFORE the message it targets.
        await _apply_raw(
            net.app_a, room_id=room, etype="m.room.member", sender="@bob:b.test",
            content={"membership": "join"}, state_key="@bob:b.test", depth=10,
        )
        msg_pdu = {
            "room_id": room, "type": "m.room.message", "sender": "@bob:b.test",
            "content": {"msgtype": "m.text", "body": "out of order"},
            "origin_server_ts": 11, "depth": 11, "prev_events": [], "auth_events": [],
        }
        msg_id = compute_event_id(msg_pdu)

        # Redaction first — stored but not yet applied (target unknown).
        red_id, red_stored = await _apply_raw(
            net.app_a, room_id=room, etype="m.room.redaction", sender="@bob:b.test",
            content={"redacts": msg_id}, depth=12,
        )
        assert red_stored
        assert await store.get_event(net.db_a, room, msg_id) is None

        # Target arrives — reconciliation scrubs it.
        assert await net.app_a.state.rooms.apply_remote_event(msg_pdu)
        target = await store.get_event(net.db_a, room, msg_id)
        assert target is not None and target.content == {}
        assert (target.unsigned or {}).get("redacted_because") == red_id

        # Re-delivering either PDU is a no-op (idempotent dedupe).
        assert await net.app_a.state.rooms.apply_remote_event(msg_pdu)
        again = await store.get_event(net.db_a, room, msg_id)
        assert again is not None and again.content == {}


async def test_inbound_redaction_requires_authority(tmp_path: Path) -> None:
    """A received redaction of another user's message by a low-power member is
    stored but NOT applied; a self-redaction is applied."""
    async with _servers(tmp_path) as net:
        alice = await _register(net.client_a, "alice")  # creator, PL 100
        room = await _public_room(net.client_a, alice)
        await _apply_raw(
            net.app_a, room_id=room, etype="m.room.member", sender="@mallory:b.test",
            content={"membership": "join"}, state_key="@mallory:b.test", depth=10,
        )

        # Alice's message; Mallory (PL 0) tries to redact it.
        sent = await net.client_a.put(
            f"{_CS}/rooms/{room}/send/m.room.message/m1",
            headers=_auth(alice),
            json={"msgtype": "m.text", "body": "keep me"},
        )
        alice_msg = sent.json()["event_id"]
        _, stored = await _apply_raw(
            net.app_a, room_id=room, etype="m.room.redaction", sender="@mallory:b.test",
            content={"redacts": alice_msg}, depth=12,
        )
        assert stored  # the redaction event is accepted...
        kept = await store.get_event(net.db_a, room, alice_msg)
        assert kept is not None and kept.content.get("body") == "keep me"  # ...but not applied

        # Mallory may redact their OWN message.
        own_id, _ = await _apply_raw(
            net.app_a, room_id=room, etype="m.room.message", sender="@mallory:b.test",
            content={"msgtype": "m.text", "body": "mine"}, depth=13,
        )
        await _apply_raw(
            net.app_a, room_id=room, etype="m.room.redaction", sender="@mallory:b.test",
            content={"redacts": own_id}, depth=14,
        )
        own = await store.get_event(net.db_a, room, own_id)
        assert own is not None and own.content == {}


async def test_make_room_admin_propagates_force_join_remote_does_not(tmp_path: Path) -> None:
    """A power-levels grant by the local creator propagates; a forced join of a
    remote user does not (we cannot sign as their server)."""
    async with _servers(tmp_path) as net:
        alice = await _register(net.client_a, "alice")  # creator on A
        room = await _public_room(net.client_a, alice)
        bob = await _register(net.client_b, "bob")
        await _join_remote(net.client_b, bob, room, "a.test")
        carol = await _register(net.client_a, "carol")
        joined = await net.client_a.post(f"{_CS}/rooms/{room}/join", headers=_auth(carol))
        assert joined.status_code == 200

        await net.app_a.state.rooms.admin_make_room_admin(room, "@carol:a.test")
        # B sees Carol elevated to PL 100.
        pls = await store.get_state_event(net.db_b, room, "m.room.power_levels", "")
        assert pls is not None and pls.content["users"]["@carol:a.test"] == 100

        # Forcing a remote user to join is applied locally but NOT propagated.
        await net.app_a.state.rooms.admin_force_join(room, "@dave:b.test")
        assert "@dave:b.test" in await store.get_joined_members(net.db_a, room)
        assert "@dave:b.test" not in await store.get_joined_members(net.db_b, room)


async def test_delete_room_with_remote_creator_is_local_only(tmp_path: Path) -> None:
    """Deleting a copy of a room whose creator is on another server tears down
    locally without forging a kick we cannot sign."""
    async with _servers(tmp_path) as net:
        bob = await _register(net.client_b, "bob")  # creator, resident on B
        room = await _public_room(net.client_b, bob)
        alice = await _register(net.client_a, "alice")
        await _join_remote(net.client_a, alice, room, "b.test")
        # A holds a copy whose creator (@bob:b.test) is remote.

        await net.app_a.state.admin.delete_room(room)

        # A's copy is gone; B is untouched (no forged kick reached it).
        assert await store.get_room(net.db_a, room) is None
        assert "@bob:b.test" in await store.get_joined_members(net.db_b, room)
        assert await _outbox_count(net.db_a, "b.test") == 0


async def test_apply_remote_event_with_state_res_v2_flag(tmp_path: Path) -> None:
    """With state_res_v2 enabled, inbound federation is authorized through the
    state-resolution path (a no-op for the linear single-extremity case today),
    and a propagated kick still applies correctly on the receiving server."""
    async with _servers(tmp_path, state_res_v2=True) as net:
        bob = await _register(net.client_b, "bob")  # resident creator on B
        room = await _public_room(net.client_b, bob)
        alice = await _register(net.client_a, "alice")
        await _join_remote(net.client_a, alice, room, "b.test")
        assert "@alice:a.test" in await store.get_joined_members(net.db_a, room)

        r = await net.client_b.post(
            f"{_CS}/rooms/{room}/kick", headers=_auth(bob), json={"user_id": "@alice:a.test"}
        )
        assert r.status_code == 200, r.text
        # A's apply_remote_event ran through state resolution and still applied the kick.
        assert "@alice:a.test" not in await store.get_joined_members(net.db_a, room)
        assert await _membership(net.db_a, room, "@alice:a.test") == "leave"
