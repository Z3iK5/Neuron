# SPDX-License-Identifier: Apache-2.0
"""Tests for neuron_server rooms, events, state, membership & auth rules (HS-2)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_BASE = "/_matrix/client/v3"


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
    resp = client.post(f"{_BASE}/createRoom", headers=_h(token), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["room_id"]


def test_create_room_sets_up_state_and_membership(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        room_id = _create_room(client, alice, name="Test Room", topic="hi")

        assert room_id.startswith("!") and room_id.endswith(":neuron.local")

        joined = client.get(f"{_BASE}/joined_rooms", headers=_h(alice)).json()["joined_rooms"]
        assert room_id in joined

        state = client.get(f"{_BASE}/rooms/{room_id}/state", headers=_h(alice)).json()
        types = {e["type"] for e in state}
        expected = {"m.room.create", "m.room.member", "m.room.power_levels", "m.room.join_rules"}
        assert expected <= types

        name = client.get(f"{_BASE}/rooms/{room_id}/state/m.room.name", headers=_h(alice)).json()
        assert name["name"] == "Test Room"

        members = client.get(
            f"{_BASE}/rooms/{room_id}/joined_members", headers=_h(alice)
        ).json()["joined"]
        assert "@alice:neuron.local" in members


def test_send_and_read_message(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        room_id = _create_room(client, alice)

        sent = client.put(
            f"{_BASE}/rooms/{room_id}/send/m.room.message/txn1",
            headers=_h(alice),
            json={"msgtype": "m.text", "body": "hello"},
        )
        assert sent.status_code == 200
        event_id = sent.json()["event_id"]

        fetched = client.get(f"{_BASE}/rooms/{room_id}/event/{event_id}", headers=_h(alice)).json()
        assert fetched["content"]["body"] == "hello"
        assert fetched["sender"] == "@alice:neuron.local"

        messages = client.get(
            f"{_BASE}/rooms/{room_id}/messages?dir=b&limit=10", headers=_h(alice)
        ).json()
        assert any(e["event_id"] == event_id for e in messages["chunk"])


def test_txn_id_makes_send_idempotent(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        room_id = _create_room(client, alice)
        url = f"{_BASE}/rooms/{room_id}/send/m.room.message/sametxn"
        body = {"msgtype": "m.text", "body": "once"}
        first = client.put(url, headers=_h(alice), json=body).json()["event_id"]
        second = client.put(url, headers=_h(alice), json=body).json()["event_id"]
        assert first == second


def test_non_member_cannot_send(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room_id = _create_room(client, alice)  # private by default

        resp = client.put(
            f"{_BASE}/rooms/{room_id}/send/m.room.message/t1",
            headers=_h(bob),
            json={"msgtype": "m.text", "body": "intrude"},
        )
        assert resp.status_code == 403 and resp.json()["errcode"] == "M_FORBIDDEN"


def test_public_room_join_then_send(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room_id = _create_room(client, alice, preset="public_chat")

        joined = client.post(f"{_BASE}/rooms/{room_id}/join", headers=_h(bob))
        assert joined.status_code == 200

        sent = client.put(
            f"{_BASE}/rooms/{room_id}/send/m.room.message/b1",
            headers=_h(bob),
            json={"msgtype": "m.text", "body": "hi from bob"},
        )
        assert sent.status_code == 200


def test_private_room_requires_invite(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room_id = _create_room(client, alice)  # private (invite-only)

        denied = client.post(f"{_BASE}/rooms/{room_id}/join", headers=_h(bob))
        assert denied.status_code == 403

        invited = client.post(
            f"{_BASE}/rooms/{room_id}/invite",
            headers=_h(alice),
            json={"user_id": "@bob:neuron.local"},
        )
        assert invited.status_code == 200

        joined = client.post(f"{_BASE}/rooms/{room_id}/join", headers=_h(bob))
        assert joined.status_code == 200


def test_power_level_gates_state_events(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room_id = _create_room(client, alice, preset="public_chat")
        client.post(f"{_BASE}/rooms/{room_id}/join", headers=_h(bob))

        # bob (PL 0) cannot set the room name (requires PL 50).
        denied = client.put(
            f"{_BASE}/rooms/{room_id}/state/m.room.name",
            headers=_h(bob),
            json={"name": "bob's name"},
        )
        assert denied.status_code == 403

        # alice raises bob to PL 50, then bob can.
        pl = client.get(
            f"{_BASE}/rooms/{room_id}/state/m.room.power_levels", headers=_h(alice)
        ).json()
        pl.setdefault("users", {})["@bob:neuron.local"] = 50
        bumped = client.put(
            f"{_BASE}/rooms/{room_id}/state/m.room.power_levels", headers=_h(alice), json=pl
        )
        assert bumped.status_code == 200

        allowed = client.put(
            f"{_BASE}/rooms/{room_id}/state/m.room.name",
            headers=_h(bob),
            json={"name": "bob's name"},
        )
        assert allowed.status_code == 200


def test_kick_and_ban(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room_id = _create_room(client, alice, preset="public_chat")
        client.post(f"{_BASE}/rooms/{room_id}/join", headers=_h(bob))

        # Kick bob; he can no longer send.
        kicked = client.post(
            f"{_BASE}/rooms/{room_id}/kick",
            headers=_h(alice),
            json={"user_id": "@bob:neuron.local"},
        )
        assert kicked.status_code == 200
        resp = client.put(
            f"{_BASE}/rooms/{room_id}/send/m.room.message/x1",
            headers=_h(bob),
            json={"msgtype": "m.text", "body": "back?"},
        )
        assert resp.status_code == 403

        # Ban bob; he cannot rejoin.
        banned = client.post(
            f"{_BASE}/rooms/{room_id}/ban", headers=_h(alice), json={"user_id": "@bob:neuron.local"}
        )
        assert banned.status_code == 200
        rejoin = client.post(f"{_BASE}/rooms/{room_id}/join", headers=_h(bob))
        assert rejoin.status_code == 403


def test_redaction_strips_content(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        room_id = _create_room(client, alice)

        event_id = client.put(
            f"{_BASE}/rooms/{room_id}/send/m.room.message/m1",
            headers=_h(alice),
            json={"msgtype": "m.text", "body": "secret"},
        ).json()["event_id"]

        redaction = client.put(
            f"{_BASE}/rooms/{room_id}/redact/{event_id}/r1",
            headers=_h(alice),
            json={"reason": "oops"},
        )
        assert redaction.status_code == 200
        redaction_id = redaction.json()["event_id"]

        after = client.get(f"{_BASE}/rooms/{room_id}/event/{event_id}", headers=_h(alice)).json()
        assert after["content"] == {}
        assert after["unsigned"]["redacted_because"] == redaction_id


def test_cannot_redact_others_event_without_power(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room_id = _create_room(client, alice, preset="public_chat")
        client.post(f"{_BASE}/rooms/{room_id}/join", headers=_h(bob))

        event_id = client.put(
            f"{_BASE}/rooms/{room_id}/send/m.room.message/m1",
            headers=_h(alice),
            json={"msgtype": "m.text", "body": "mine"},
        ).json()["event_id"]

        denied = client.put(
            f"{_BASE}/rooms/{room_id}/redact/{event_id}/r1", headers=_h(bob), json={}
        )
        assert denied.status_code == 403


def test_unknown_room_is_not_found(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        resp = client.get(
            f"{_BASE}/rooms/!nope:neuron.local/state", headers=_h(alice)
        )
        assert resp.status_code == 404 and resp.json()["errcode"] == "M_NOT_FOUND"
