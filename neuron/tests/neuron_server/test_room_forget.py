# SPDX-License-Identifier: Apache-2.0
"""Tests for POST /rooms/{room_id}/forget: rejection while joined, hiding the
room from /sync after forgetting, and re-join clearing the flag."""

from __future__ import annotations

from pathlib import Path

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


def _register(client: TestClient, username: str) -> tuple[str, str]:
    challenge = client.post(_REG, json={"username": username, "password": "pw-123456"})
    session = challenge.json()["session"]
    out = client.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    ).json()
    return out["access_token"], out["user_id"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _public_room(client: TestClient, token: str) -> str:
    return client.post(
        f"{_B}/createRoom", headers=_h(token), json={"preset": "public_chat"}
    ).json()["room_id"]


def _sync(client: TestClient, token: str, since: str | None = None) -> dict:
    query = f"?since={since}&timeout=0" if since else ""
    return client.get(f"{_B}/sync{query}", headers=_h(token)).json()


def test_forget_while_joined_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice, _ = _register(client, "alice")
        bob, _ = _register(client, "bob")
        room = _public_room(client, alice)
        assert client.post(f"{_B}/rooms/{room}/join", headers=_h(bob)).status_code == 200

        resp = client.post(f"{_B}/rooms/{room}/forget", headers=_h(bob))
        assert resp.status_code == 400
        assert resp.json()["errcode"] == "M_UNKNOWN"


def test_forget_without_membership_is_not_found(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice, _ = _register(client, "alice")
        bob, _ = _register(client, "bob")
        room = _public_room(client, alice)
        resp = client.post(f"{_B}/rooms/{room}/forget", headers=_h(bob))
        assert resp.status_code == 404


def test_forget_hides_left_room_from_sync_and_rejoin_clears_it(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice, _ = _register(client, "alice")
        bob, _ = _register(client, "bob")
        room = _public_room(client, alice)
        assert client.post(f"{_B}/rooms/{room}/join", headers=_h(bob)).status_code == 200

        since = _sync(client, bob)["next_batch"]
        assert client.post(f"{_B}/rooms/{room}/leave", headers=_h(bob)).status_code == 200

        # Before forgetting, the leave shows up in an incremental sync.
        assert room in _sync(client, bob, since)["rooms"]["leave"]

        assert client.post(f"{_B}/rooms/{room}/forget", headers=_h(bob)).status_code == 200

        # After forgetting, the same incremental sync no longer mentions the room.
        body = _sync(client, bob, since)
        assert room not in body["rooms"]["leave"]
        assert room not in body["rooms"]["join"]

        # Re-joining clears the forgotten flag: the room is visible again...
        assert client.post(f"{_B}/rooms/{room}/join", headers=_h(bob)).status_code == 200
        assert room in client.get(f"{_B}/joined_rooms", headers=_h(bob)).json()["joined_rooms"]
        assert room in _sync(client, bob)["rooms"]["join"]

        # ...including a later leave, which reappears in the leave section.
        since = _sync(client, bob)["next_batch"]
        assert client.post(f"{_B}/rooms/{room}/leave", headers=_h(bob)).status_code == 200
        assert room in _sync(client, bob, since)["rooms"]["leave"]
