# SPDX-License-Identifier: Apache-2.0
"""Tests for the remaining CS API (HS-6): profile, account data, capabilities,
filters, and the accepted-but-stubbed presence/typing/receipt endpoints."""

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


def test_profile_displayname_roundtrip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        put = client.put(
            f"{_B}/profile/{user_id}/displayname", headers=_h(token), json={"displayname": "Alice"}
        )
        assert put.status_code == 200
        got = client.get(f"{_B}/profile/{user_id}").json()
        assert got["displayname"] == "Alice"

        # Cannot edit another user's profile.
        denied = client.put(
            f"{_B}/profile/@bob:neuron.local/displayname",
            headers=_h(token),
            json={"displayname": "x"},
        )
        assert denied.status_code == 403


def test_account_data_roundtrip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        url = f"{_B}/user/{user_id}/account_data/m.test"
        assert client.get(url, headers=_h(token)).status_code == 404
        assert client.put(url, headers=_h(token), json={"foo": "bar"}).status_code == 200
        assert client.get(url, headers=_h(token)).json() == {"foo": "bar"}


def test_filter_roundtrip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        created = client.post(
            f"{_B}/user/{user_id}/filter",
            headers=_h(token),
            json={"room": {"timeline": {"limit": 5}}},
        ).json()
        filter_id = created["filter_id"]
        fetched = client.get(f"{_B}/user/{user_id}/filter/{filter_id}", headers=_h(token)).json()
        assert fetched["room"]["timeline"]["limit"] == 5


def test_capabilities_and_pushrules(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _user = _register(client, "alice")
        caps = client.get(f"{_B}/capabilities", headers=_h(token)).json()
        assert "11" in caps["capabilities"]["m.room_versions"]["available"]
        rules = client.get(f"{_B}/pushrules/", headers=_h(token)).json()
        assert "global" in rules


def test_typing_and_receipts_accepted(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        assert client.put(
            f"{_B}/rooms/!r:neuron.local/typing/{user_id}",
            headers=_h(token),
            json={"typing": True, "timeout": 30000},
        ).status_code == 200
        assert client.post(
            f"{_B}/rooms/!r:neuron.local/receipt/m.read/$abc", headers=_h(token), json={}
        ).status_code == 200


def _create_room_with_message(client: TestClient, token: str) -> tuple[str, str]:
    room = client.post(f"{_B}/createRoom", headers=_h(token), json={}).json()["room_id"]
    event_id = client.put(
        f"{_B}/rooms/{room}/send/m.room.message/t1",
        headers=_h(token),
        json={"msgtype": "m.text", "body": "hi"},
    ).json()["event_id"]
    return room, event_id


def test_read_markers_stores_fully_read_and_receipts(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        room, event_id = _create_room_with_message(client, token)

        resp = client.post(
            f"{_B}/rooms/{room}/read_markers",
            headers=_h(token),
            json={
                "m.fully_read": event_id,
                "m.read": event_id,
                "m.read.private": event_id,
            },
        )
        assert resp.status_code == 200

        # m.fully_read roams as per-room account data.
        marker = client.get(
            f"{_B}/user/{user_id}/rooms/{room}/account_data/m.fully_read", headers=_h(token)
        ).json()
        assert marker == {"event_id": event_id}
        sync = client.get(f"{_B}/sync?timeout=0", headers=_h(token)).json()
        room_account = sync["rooms"]["join"][room]["account_data"]["events"]
        assert {"type": "m.fully_read", "content": {"event_id": event_id}} in room_account

        # Both receipts are stored and surfaced to the owner in sync ephemeral.
        receipts = next(
            e["content"]
            for e in sync["rooms"]["join"][room]["ephemeral"]["events"]
            if e["type"] == "m.receipt"
        )
        assert user_id in receipts[event_id]["m.read"]
        assert user_id in receipts[event_id]["m.read.private"]


def test_read_markers_with_only_fully_read(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        room, event_id = _create_room_with_message(client, token)

        assert client.post(
            f"{_B}/rooms/{room}/read_markers",
            headers=_h(token),
            json={"m.fully_read": event_id},
        ).status_code == 200
        marker = client.get(
            f"{_B}/user/{user_id}/rooms/{room}/account_data/m.fully_read", headers=_h(token)
        ).json()
        assert marker == {"event_id": event_id}


def test_receipt_private_persisted_but_unknown_types_ignored(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        room, event_id = _create_room_with_message(client, token)

        assert client.post(
            f"{_B}/rooms/{room}/receipt/m.read.private/{event_id}", headers=_h(token), json={}
        ).status_code == 200
        assert client.post(
            f"{_B}/rooms/{room}/receipt/m.bogus/{event_id}", headers=_h(token), json={}
        ).status_code == 200  # accepted but not persisted

        sync = client.get(f"{_B}/sync?timeout=0", headers=_h(token)).json()
        receipts = next(
            e["content"]
            for e in sync["rooms"]["join"][room]["ephemeral"]["events"]
            if e["type"] == "m.receipt"
        )
        assert user_id in receipts[event_id]["m.read.private"]
        assert "m.bogus" not in receipts[event_id]


def test_typing_rejects_non_integer_timeout(tmp_path: Path) -> None:
    """A malformed timeout must produce a spec-shaped 400, not an unhandled 500."""
    with _client(tmp_path) as client:
        token, user_id = _register(client, "alice")
        for bad_timeout in ("abc", None, [1], {"ms": 5}):
            resp = client.put(
                f"{_B}/rooms/!r:neuron.local/typing/{user_id}",
                headers=_h(token),
                json={"typing": True, "timeout": bad_timeout},
            )
            assert resp.status_code == 400
            assert resp.json()["errcode"] == "M_INVALID_PARAM"
