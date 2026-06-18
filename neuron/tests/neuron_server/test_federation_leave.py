# SPDX-License-Identifier: Apache-2.0
"""Federated leave (HS-7 step 6d).

A user on server A joins a room hosted by B and then leaves it through the ordinary
Client-Server leave endpoint. The leave propagates to B (the resident server) and
is reflected in A's local copy of the room.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.storage import rooms as store

_CS = "/_matrix/client/v3"
_ALICE = "@alice:a.test"


def _opener(target_app: object):  # noqa: ANN202 - test helper
    def open_client(server_name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target_app), base_url=f"https://{server_name}"
        )

    return open_client


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


async def test_local_user_leaves_remote_room(tmp_path: Path) -> None:
    app_a = create_app(
        NeuronServerSettings(name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}")
    )
    app_b = create_app(
        NeuronServerSettings(name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
    )

    async with app_b.router.lifespan_context(app_b), app_a.router.lifespan_context(app_a):
        app_a.state.federation_client.open_client = _opener(app_b)
        app_b.state.federation_client.open_client = _opener(app_a)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
        ) as on_b:
            bob = await _register(on_b, "bob")
            room_id = (
                await on_b.post(
                    f"{_CS}/createRoom",
                    headers={"Authorization": f"Bearer {bob}"},
                    json={"preset": "public_chat"},
                )
            ).json()["room_id"]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        ) as on_a:
            alice = await _register(on_a, "alice")
            headers = {"Authorization": f"Bearer {alice}"}

            join = await on_a.post(
                f"{_CS}/rooms/{room_id}/join", params={"server_name": "b.test"}, headers=headers
            )
            assert join.status_code == 200
            assert room_id in (
                await on_a.get(f"{_CS}/joined_rooms", headers=headers)
            ).json()["joined_rooms"]

            # Now leave the remote room.
            left = await on_a.post(f"{_CS}/rooms/{room_id}/leave", headers=headers)
            assert left.status_code == 200

            # A's local copy no longer lists the room as joined.
            assert room_id not in (
                await on_a.get(f"{_CS}/joined_rooms", headers=headers)
            ).json()["joined_rooms"]

        # B (the resident) sees our user gone from the room.
        assert _ALICE not in await store.get_joined_members(app_b.state.db, room_id)  # type: ignore[attr-defined]
        # B still hosts the room with its creator.
        assert "@bob:b.test" in await store.get_joined_members(app_b.state.db, room_id)  # type: ignore[attr-defined]
