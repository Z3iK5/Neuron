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
