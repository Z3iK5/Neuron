# SPDX-License-Identifier: Apache-2.0
"""Hub fanout (HS-7): the room's resident server relays accepted remote events to
the room's *other* remote servers.

Three servers share a room hosted on ``a.test``: an event that reaches the hub
from ``b.test`` must be relayed on to ``c.test`` (and never back to ``b.test``),
and membership changes accepted via send_join / send_leave / the invite flow must
fan out too — otherwise a 3-server room silently drops cross-server traffic.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_CS = "/_matrix/client/v3"


def _opener(apps: dict[str, FastAPI], log: list[str] | None = None):  # noqa: ANN202
    """Route a destination server name to its in-process app, recording opens."""

    def open_client(server_name: str) -> httpx.AsyncClient:
        if log is not None:
            log.append(server_name)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[server_name]),
            base_url=f"https://{server_name}",
        )

    return open_client


async def _register(client: httpx.AsyncClient, username: str) -> dict[str, str]:
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
    return {"Authorization": f"Bearer {out.json()['access_token']}"}


def _timeline_bodies(sync_json: dict, room_id: str) -> list[str]:
    room = sync_json["rooms"]["join"].get(room_id, {})
    events = room.get("timeline", {}).get("events", [])
    return [e["content"].get("body") for e in events if e["type"] == "m.room.message"]


async def _memberships(
    client: httpx.AsyncClient, room_id: str, headers: dict[str, str]
) -> dict[str, str]:
    out = (await client.get(f"{_CS}/rooms/{room_id}/members", headers=headers)).json()
    return {e["state_key"]: e["content"]["membership"] for e in out.get("chunk", [])}


async def test_hub_fanout_three_servers(tmp_path: Path) -> None:
    apps: dict[str, FastAPI] = {
        name: create_app(
            NeuronServerSettings(
                name=name, database_url=f"sqlite:///{tmp_path / f'{name}.db'}"
            )
        )
        for name in ("a.test", "b.test", "c.test")
    }
    a_opens: list[str] = []
    b_opens: list[str] = []

    async with (
        apps["a.test"].router.lifespan_context(apps["a.test"]),
        apps["b.test"].router.lifespan_context(apps["b.test"]),
        apps["c.test"].router.lifespan_context(apps["c.test"]),
    ):
        apps["a.test"].state.federation_client.open_client = _opener(apps, a_opens)
        apps["b.test"].state.federation_client.open_client = _opener(apps, b_opens)
        apps["c.test"].state.federation_client.open_client = _opener(apps)

        clients = {
            name: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=f"https://{name}"
            )
            for name, app in apps.items()
        }
        try:
            alice_h = await _register(clients["a.test"], "alice")
            bob_h = await _register(clients["b.test"], "bob")
            carol_h = await _register(clients["c.test"], "carol")

            room_id = (
                await clients["a.test"].post(
                    f"{_CS}/createRoom", headers=alice_h, json={"preset": "public_chat"}
                )
            ).json()["room_id"]

            await clients["b.test"].post(
                f"{_CS}/rooms/{room_id}/join",
                params={"server_name": "a.test"},
                headers=bob_h,
            )
            await clients["c.test"].post(
                f"{_CS}/rooms/{room_id}/join",
                params={"server_name": "a.test"},
                headers=carol_h,
            )

            # Join fanout: the hub told B about carol's join (which B never saw
            # directly), so B's copy of the member list includes her.
            b_members = await _memberships(clients["b.test"], room_id, bob_h)
            assert b_members.get("@carol:c.test") == "join"

            # A message from B reaches C through the hub...
            a_opens.clear()
            await clients["b.test"].put(
                f"{_CS}/rooms/{room_id}/send/m.room.message/m1",
                headers=bob_h,
                json={"msgtype": "m.text", "body": "hello from bob"},
            )
            carol_sync = (
                await clients["c.test"].get(f"{_CS}/sync", headers=carol_h)
            ).json()
            assert "hello from bob" in _timeline_bodies(carol_sync, room_id)
            # ...and the hub never sent it back to its origin.
            assert "c.test" in a_opens
            assert "b.test" not in a_opens

            # Non-hub servers do not relay: an event arriving at B (sent by the
            # hub's own user) triggers no outbound federation traffic from B.
            b_opens.clear()
            await clients["a.test"].put(
                f"{_CS}/rooms/{room_id}/send/m.room.message/m2",
                headers=alice_h,
                json={"msgtype": "m.text", "body": "hello from alice"},
            )
            bob_sync = (await clients["b.test"].get(f"{_CS}/sync", headers=bob_h)).json()
            assert "hello from alice" in _timeline_bodies(bob_sync, room_id)
            assert b_opens == []

            # Invite fanout: alice invites a user on C; B learns about the invite.
            await clients["a.test"].post(
                f"{_CS}/rooms/{room_id}/invite",
                headers=alice_h,
                json={"user_id": "@dave:c.test"},
            )
            b_members = await _memberships(clients["b.test"], room_id, bob_h)
            assert b_members.get("@dave:c.test") == "invite"

            # Leave fanout: carol leaves via the hub; B sees her go.
            await clients["c.test"].post(
                f"{_CS}/rooms/{room_id}/leave", headers=carol_h
            )
            b_members = await _memberships(clients["b.test"], room_id, bob_h)
            assert b_members.get("@carol:c.test") == "leave"
        finally:
            for client in clients.values():
                await client.aclose()


async def test_two_server_room_does_not_echo_to_origin(tmp_path: Path) -> None:
    """With only the hub and one remote, an inbound event triggers no relay at all."""
    apps: dict[str, FastAPI] = {
        name: create_app(
            NeuronServerSettings(
                name=name, database_url=f"sqlite:///{tmp_path / f'{name}.db'}"
            )
        )
        for name in ("a.test", "b.test")
    }
    a_opens: list[str] = []

    async with (
        apps["a.test"].router.lifespan_context(apps["a.test"]),
        apps["b.test"].router.lifespan_context(apps["b.test"]),
    ):
        apps["a.test"].state.federation_client.open_client = _opener(apps, a_opens)
        apps["b.test"].state.federation_client.open_client = _opener(apps)

        clients = {
            name: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=f"https://{name}"
            )
            for name, app in apps.items()
        }
        try:
            alice_h = await _register(clients["a.test"], "alice")
            bob_h = await _register(clients["b.test"], "bob")
            room_id = (
                await clients["a.test"].post(
                    f"{_CS}/createRoom", headers=alice_h, json={"preset": "public_chat"}
                )
            ).json()["room_id"]
            await clients["b.test"].post(
                f"{_CS}/rooms/{room_id}/join",
                params={"server_name": "a.test"},
                headers=bob_h,
            )

            a_opens.clear()
            await clients["b.test"].put(
                f"{_CS}/rooms/{room_id}/send/m.room.message/m1",
                headers=bob_h,
                json={"msgtype": "m.text", "body": "only us"},
            )
            # The hub accepted the event but had nobody else to relay it to — and
            # crucially never echoed it back to b.test (no ping-pong).
            assert "b.test" not in a_opens
        finally:
            for client in clients.values():
                await client.aclose()
