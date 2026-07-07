# SPDX-License-Identifier: Apache-2.0
"""Tests for Simplified Sliding Sync (MSC4186): windowing, deltas, extensions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_B = "/_matrix/client/v3"
_SS = "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"


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


def _send(client: TestClient, token: str, room: str, text: str, txn: str) -> str:
    return client.put(
        f"{_B}/rooms/{room}/send/m.room.message/{txn}",
        headers=_h(token),
        json={"msgtype": "m.text", "body": text},
    ).json()["event_id"]


def _ss(client: TestClient, token: str, body: dict[str, Any], **params: Any) -> dict[str, Any]:
    url = _SS
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return client.post(url, headers=_h(token), json=body).json()


def _all_list(**extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {"ranges": [[0, 10]], "timeline_limit": 10}
    spec.update(extra)
    return {"lists": {"all": spec}}


def test_initial_sync_windows_rooms_by_recency(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        older = _create_room(client, alice, name="Older")
        newer = _create_room(client, alice, name="Newer")
        _send(client, alice, older, "bump older", "t1")  # older now most recent

        body = _ss(client, alice, _all_list(required_state=[["m.room.create", ""]]))
        assert "pos" in body
        assert body["lists"]["all"]["count"] == 2
        # older was bumped last, so it sorts ahead of newer.
        assert list(body["rooms"]) == [older, newer]

        room = body["rooms"][older]
        assert room["initial"] is True
        assert room["name"] == "Older"
        state_types = {e["type"] for e in room["required_state"]}
        assert "m.room.create" in state_types
        # timeline is a flat list of events in MSC4186 (not wrapped in {events}).
        timeline_bodies = [e.get("content", {}).get("body") for e in room["timeline"]]
        assert "bump older" in timeline_bodies


def test_delta_returns_only_new_activity(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        room = _create_room(client, alice)

        first = _ss(client, alice, _all_list())
        pos = first["pos"]
        assert room in first["rooms"]

        # Nothing new -> room omitted on the delta.
        empty = _ss(client, alice, _all_list(), pos=pos)
        assert room not in empty["rooms"]

        _send(client, alice, room, "new one", "t1")
        delta = _ss(client, alice, _all_list(), pos=empty["pos"])
        assert room in delta["rooms"]
        assert delta["rooms"][room]["initial"] is False
        bodies = [e.get("content", {}).get("body") for e in delta["rooms"][room]["timeline"]]
        assert bodies == ["new one"]


def test_ranges_window_and_count(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        rooms = []
        for i in range(4):
            r = _create_room(client, alice, name=f"R{i}")
            _send(client, alice, r, "hi", f"t{i}")
            rooms.append(r)
        # Recency order is reverse creation order (last created bumped last).
        expected_order = list(reversed(rooms))

        body = _ss(client, alice, {"lists": {"all": {"ranges": [[0, 1]], "timeline_limit": 5}}})
        assert body["lists"]["all"]["count"] == 4  # full filtered total, not the window
        assert list(body["rooms"]) == expected_order[:2]


def test_room_subscription_included_outside_window(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        a = _create_room(client, alice, name="A")
        b = _create_room(client, alice, name="B")  # most recent
        # A window of just the top room excludes A; subscribe to A explicitly.
        body = _ss(
            client,
            alice,
            {
                "lists": {"all": {"ranges": [[0, 0]], "timeline_limit": 5}},
                "room_subscriptions": {a: {"timeline_limit": 5}},
            },
        )
        assert b in body["rooms"]
        assert a in body["rooms"]  # present despite falling outside the list window


def test_required_state_lazy_loads_timeline_senders_and_me(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room = _create_room(client, alice, preset="public_chat")
        client.post(f"{_B}/rooms/{room}/join", headers=_h(bob))
        _send(client, bob, room, "from bob", "t1")  # only bob speaks in the timeline

        body = _ss(
            client,
            alice,
            _all_list(required_state=[["m.room.member", "$LAZY"]]),
        )
        members = {
            e["state_key"]
            for e in body["rooms"][room]["required_state"]
            if e["type"] == "m.room.member"
        }
        # $LAZY: the timeline sender (bob) plus the syncing user (alice), never a
        # third uninvolved member.
        assert members == {"@alice:neuron.local", "@bob:neuron.local"}


def test_extensions_round_trip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob = _register(client, "bob")
        room = _create_room(client, alice, preset="public_chat")
        client.post(f"{_B}/rooms/{room}/join", headers=_h(bob))
        event_id = _send(client, alice, room, "read me", "t1")

        # Account data + receipt to exercise those extensions.
        client.put(
            f"{_B}/user/@bob:neuron.local/account_data/m.test",
            headers=_h(bob),
            json={"k": "v"},
        )
        client.post(f"{_B}/rooms/{room}/receipt/m.read/{event_id}", headers=_h(bob), json={})

        body = _ss(
            client,
            bob,
            {
                "lists": {"all": {"ranges": [[0, 10]], "timeline_limit": 5}},
                "extensions": {
                    "to_device": {"enabled": True},
                    "e2ee": {"enabled": True},
                    "account_data": {"enabled": True},
                    "receipts": {"enabled": True},
                    "typing": {"enabled": True},
                },
            },
        )
        ext = body["extensions"]
        assert "next_batch" in ext["to_device"]
        assert "device_one_time_keys_count" in ext["e2ee"]
        assert {"type": "m.test", "content": {"k": "v"}} in ext["account_data"]["global"]
        assert "@bob:neuron.local" in ext["receipts"]["rooms"][room][event_id]["m.read"]
        assert "rooms" in ext["typing"]

        # Disabled extensions are omitted entirely.
        none_body = _ss(client, bob, _all_list())
        assert none_body["extensions"] == {}


def test_to_device_extension_drains(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        bob_token = _register(client, "bob")
        # Discover bob's device id via whoami-style login info: use /sync device.
        bob_devices = client.get(f"{_B}/devices", headers=_h(bob_token)).json()["devices"]
        bob_device = bob_devices[0]["device_id"]

        client.put(
            f"{_B}/sendToDevice/m.room_key/txn1",
            headers=_h(alice),
            json={"messages": {"@bob:neuron.local": {bob_device: {"hello": "there"}}}},
        )
        body = _ss(
            client,
            bob_token,
            {"extensions": {"to_device": {"enabled": True}}},
        )
        events = body["extensions"]["to_device"]["events"]
        assert any(e["content"] == {"hello": "there"} for e in events)


def test_unknown_pos_rebuilds_as_initial(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice = _register(client, "alice")
        room = _create_room(client, alice)
        _ss(client, alice, _all_list())  # establish a connection

        rebuilt = _ss(client, alice, _all_list(), pos="deadbeef_999")
        assert room in rebuilt["rooms"]
        assert rebuilt["rooms"][room]["initial"] is True


async def test_long_poll_wakes_on_new_message(tmp_path: Path) -> None:
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
            created = await c.post(f"{_B}/createRoom", headers=headers, json={})
            room = created.json()["room_id"]

            body = {"lists": {"all": {"ranges": [[0, 10]], "timeline_limit": 5}}}
            first = (await c.post(_SS, headers=headers, json=body)).json()
            pos = first["pos"]

            task = asyncio.create_task(
                c.post(f"{_SS}?pos={pos}&timeout=10000", headers=headers, json=body)
            )
            await asyncio.sleep(0.2)
            await c.put(
                f"{_B}/rooms/{room}/send/m.room.message/t1",
                headers=headers,
                json={"msgtype": "m.text", "body": "ping"},
            )
            response = await asyncio.wait_for(task, timeout=5)

        woken = response.json()
        assert room in woken["rooms"]
        bodies = [e.get("content", {}).get("body") for e in woken["rooms"][room]["timeline"]]
        assert "ping" in bodies
