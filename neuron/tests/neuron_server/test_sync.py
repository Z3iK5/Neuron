# SPDX-License-Identifier: Apache-2.0
"""Tests for neuron_server GET /sync (HS-3): initial, incremental, long-poll."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_B = "/_matrix/client/v3"


def _client(tmp_path: Path) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    return TestClient(create_app(settings))


def _register(client: TestClient, username: str) -> str:
    challenge = client.post(_REG, json={"username": username, "password": "pw-123456"})
    session = challenge.json()["session"]
    result = client.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    )
    return result.json()["access_token"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_room(client: TestClient, token: str, **body: object) -> str:
    return client.post(f"{_B}/createRoom", headers=_h(token), json=body).json()["room_id"]


def _send(client: TestClient, token: str, room: str, body_text: str, txn: str) -> str:
    return client.put(
        f"{_B}/rooms/{room}/send/m.room.message/{txn}",
        headers=_h(token),
        json={"msgtype": "m.text", "body": body_text},
    ).json()["event_id"]


def _room_account_data(body: dict, room: str) -> list[dict]:
    return body["rooms"]["join"][room]["account_data"]["events"]


def _receipt_content(body: dict, room: str) -> dict:
    for event in body["rooms"]["join"].get(room, {}).get("ephemeral", {}).get("events", []):
        if event["type"] == "m.receipt":
            return event["content"]
    return {}


def _unread(body: dict, room: str) -> dict:
    return body["rooms"]["join"][room]["unread_notifications"]


def test_initial_sync_includes_joined_room_state_and_timeline(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        room = _create_room(client, alice, name="Lobby")
        client.put(
            f"{_B}/rooms/{room}/send/m.room.message/t1",
            headers=_h(alice),
            json={"msgtype": "m.text", "body": "hello"},
        )

        body = client.get(f"{_B}/sync?timeout=0", headers=_h(alice)).json()
        assert "next_batch" in body
        assert room in body["rooms"]["join"]

        joined = body["rooms"]["join"][room]
        state_types = {e["type"] for e in joined["state"]["events"]}
        assert "m.room.create" in state_types and "m.room.member" in state_types
        bodies = [e.get("content", {}).get("body") for e in joined["timeline"]["events"]]
        assert "hello" in bodies


def test_incremental_sync_returns_only_new_events(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        room = _create_room(client, alice)

        token = client.get(f"{_B}/sync?timeout=0", headers=_h(alice)).json()["next_batch"]

        # Nothing new yet -> room absent from join.
        empty = client.get(f"{_B}/sync?since={token}&timeout=0", headers=_h(alice)).json()
        assert room not in empty["rooms"]["join"]

        client.put(
            f"{_B}/rooms/{room}/send/m.room.message/t1",
            headers=_h(alice),
            json={"msgtype": "m.text", "body": "new"},
        )
        inc = client.get(f"{_B}/sync?since={token}&timeout=0", headers=_h(alice)).json()
        assert room in inc["rooms"]["join"]
        bodies = [
            e.get("content", {}).get("body")
            for e in inc["rooms"]["join"][room]["timeline"]["events"]
        ]
        assert bodies == ["new"]


def test_invited_room_appears_in_sync(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room = _create_room(client, alice)  # private
        client.post(
            f"{_B}/rooms/{room}/invite",
            headers=_h(alice),
            json={"user_id": "@bob:neuron.local"},
        )

        body = client.get(f"{_B}/sync?timeout=0", headers=_h(bob)).json()
        assert room in body["rooms"]["invite"]
        types = {e["type"] for e in body["rooms"]["invite"][room]["invite_state"]["events"]}
        assert "m.room.create" in types


def test_leave_appears_in_incremental_sync(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room = _create_room(client, alice, preset="public_chat")
        client.post(f"{_B}/rooms/{room}/join", headers=_h(bob))

        token = client.get(f"{_B}/sync?timeout=0", headers=_h(bob)).json()["next_batch"]
        client.post(
            f"{_B}/rooms/{room}/kick",
            headers=_h(alice),
            json={"user_id": "@bob:neuron.local"},
        )

        body = client.get(f"{_B}/sync?since={token}&timeout=0", headers=_h(bob)).json()
        assert room in body["rooms"]["leave"]


def test_global_account_data_in_initial_and_incremental_sync(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        client.put(
            f"{_B}/user/@alice:neuron.local/account_data/m.test",
            headers=_h(alice),
            json={"colour": "blue"},
        )

        initial = client.get(f"{_B}/sync?timeout=0", headers=_h(alice)).json()
        events = initial["account_data"]["events"]
        assert {"type": "m.test", "content": {"colour": "blue"}} in events
        token = initial["next_batch"]

        # Unchanged -> not repeated on incremental sync.
        empty = client.get(f"{_B}/sync?since={token}&timeout=0", headers=_h(alice)).json()
        assert empty["account_data"]["events"] == []

        # A new write appears exactly once.
        client.put(
            f"{_B}/user/@alice:neuron.local/account_data/m.test",
            headers=_h(alice),
            json={"colour": "red"},
        )
        inc = client.get(f"{_B}/sync?since={token}&timeout=0", headers=_h(alice)).json()
        assert inc["account_data"]["events"] == [
            {"type": "m.test", "content": {"colour": "red"}}
        ]
        again = client.get(
            f"{_B}/sync?since={inc['next_batch']}&timeout=0", headers=_h(alice)
        ).json()
        assert again["account_data"]["events"] == []


def test_room_account_data_in_sync(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        room = _create_room(client, alice)
        client.put(
            f"{_B}/user/@alice:neuron.local/rooms/{room}/account_data/m.tag_order",
            headers=_h(alice),
            json={"order": 1},
        )

        initial = client.get(f"{_B}/sync?timeout=0", headers=_h(alice)).json()
        assert _room_account_data(initial, room) == [
            {"type": "m.tag_order", "content": {"order": 1}}
        ]
        assert initial["account_data"]["events"] == []  # room data is not global
        token = initial["next_batch"]

        # A room account-data write surfaces the room on incremental sync.
        client.put(
            f"{_B}/user/@alice:neuron.local/rooms/{room}/account_data/m.tag_order",
            headers=_h(alice),
            json={"order": 2},
        )
        inc = client.get(f"{_B}/sync?since={token}&timeout=0", headers=_h(alice)).json()
        assert _room_account_data(inc, room) == [
            {"type": "m.tag_order", "content": {"order": 2}}
        ]


def test_private_receipt_visible_to_owner_but_never_leaked(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room = _create_room(client, alice, preset="public_chat")
        client.post(f"{_B}/rooms/{room}/join", headers=_h(bob))
        event_id = _send(client, alice, room, "secret read", "t1")

        assert client.post(
            f"{_B}/rooms/{room}/receipt/m.read.private/{event_id}", headers=_h(bob), json={}
        ).status_code == 200

        # Bob (the owner) sees his own private receipt...
        own = client.get(f"{_B}/sync?timeout=0", headers=_h(bob)).json()
        assert "@bob:neuron.local" in _receipt_content(own, room).get(event_id, {}).get(
            "m.read.private", {}
        )
        # ...but Alice must never see it.
        other = client.get(f"{_B}/sync?timeout=0", headers=_h(alice)).json()
        assert "m.read.private" not in str(_receipt_content(other, room))


def test_unread_notifications_count_highlight_and_reset(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room = _create_room(client, alice, preset="public_chat")
        _send(client, alice, room, "before bob joined", "t0")
        client.post(f"{_B}/rooms/{room}/join", headers=_h(bob))

        _send(client, alice, room, "hello there", "t1")
        last = _send(client, alice, room, "are you around, bob?", "t2")

        body = client.get(f"{_B}/sync?timeout=0", headers=_h(bob)).json()
        # Only messages after bob's join count; the mention of "bob" highlights.
        assert _unread(body, room) == {"notification_count": 2, "highlight_count": 1}

        # The sender's own messages never count against them.
        alice_body = client.get(f"{_B}/sync?timeout=0", headers=_h(alice)).json()
        assert _unread(alice_body, room) == {"notification_count": 0, "highlight_count": 0}

        # Reading the latest event resets the counts.
        client.post(f"{_B}/rooms/{room}/receipt/m.read/{last}", headers=_h(bob), json={})
        after = client.get(f"{_B}/sync?timeout=0", headers=_h(bob)).json()
        assert _unread(after, room) == {"notification_count": 0, "highlight_count": 0}

        # New messages start counting again from the receipt.
        _send(client, alice, room, "one more", "t3")
        more = client.get(f"{_B}/sync?timeout=0", headers=_h(bob)).json()
        assert _unread(more, room) == {"notification_count": 1, "highlight_count": 0}


def test_unread_highlight_matches_display_name(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        client.put(
            f"{_B}/profile/@bob:neuron.local/displayname",
            headers=_h(bob),
            json={"displayname": "Bobby Tables"},
        )
        room = _create_room(client, alice, preset="public_chat")
        client.post(f"{_B}/rooms/{room}/join", headers=_h(bob))
        _send(client, alice, room, "lunch, BOBBY TABLES?", "t1")  # case-insensitive

        body = client.get(f"{_B}/sync?timeout=0", headers=_h(bob)).json()
        assert _unread(body, room) == {"notification_count": 1, "highlight_count": 1}


async def test_long_poll_wakes_on_account_data_write(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hs.test") as c:
            challenge = await c.post(_REG, json={"username": "alice", "password": "pw-123456"})
            session = challenge.json()["session"]
            reg = await c.post(
                _REG,
                json={
                    "username": "alice",
                    "password": "pw-123456",
                    "auth": {"type": "m.login.dummy", "session": session},
                },
            )
            headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
            since = (await c.get(f"{_B}/sync?timeout=0", headers=headers)).json()["next_batch"]

            sync_task = asyncio.create_task(
                c.get(f"{_B}/sync?since={since}&timeout=10000", headers=headers)
            )
            await asyncio.sleep(0.2)
            await c.put(
                f"{_B}/user/@alice:neuron.local/account_data/m.test",
                headers=headers,
                json={"woken": True},
            )
            response = await asyncio.wait_for(sync_task, timeout=5)

        assert response.json()["account_data"]["events"] == [
            {"type": "m.test", "content": {"woken": True}}
        ]


async def test_long_poll_wakes_on_new_message(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    app = create_app(settings)
    base = "http://hs.test"

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base) as c:
            challenge = await c.post(_REG, json={"username": "alice", "password": "pw-123456"})
            session = challenge.json()["session"]
            reg = await c.post(
                _REG,
                json={
                    "username": "alice",
                    "password": "pw-123456",
                    "auth": {"type": "m.login.dummy", "session": session},
                },
            )
            token = reg.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            created = await c.post(f"{_B}/createRoom", headers=headers, json={})
            room = created.json()["room_id"]
            since = (await c.get(f"{_B}/sync?timeout=0", headers=headers)).json()["next_batch"]

            # Start a long-poll, then send a message from a concurrent task.
            sync_task = asyncio.create_task(
                c.get(f"{_B}/sync?since={since}&timeout=10000", headers=headers)
            )
            await asyncio.sleep(0.2)
            await c.put(
                f"{_B}/rooms/{room}/send/m.room.message/t1",
                headers=headers,
                json={"msgtype": "m.text", "body": "ping"},
            )
            response = await asyncio.wait_for(sync_task, timeout=5)

        body = response.json()
        assert room in body["rooms"]["join"]
        bodies = [
            e.get("content", {}).get("body")
            for e in body["rooms"]["join"][room]["timeline"]["events"]
        ]
        assert "ping" in bodies
