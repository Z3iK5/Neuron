# SPDX-License-Identifier: Apache-2.0
"""Outbound federated join (HS-7 step 6b).

A user on server A joins a room hosted by server B through the ordinary Client-
Server join endpoint. A runs the make_join/send_join handshake against B and then
**persists B's room locally**, so afterwards A's user is joined and can read the
room as if it were local.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_CS = "/_matrix/client/v3"


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


async def test_local_user_joins_remote_room(tmp_path: Path) -> None:
    app_a = create_app(
        NeuronServerSettings(name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}")
    )
    app_b = create_app(
        NeuronServerSettings(name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
    )

    async with app_b.router.lifespan_context(app_b), app_a.router.lifespan_context(app_a):
        # Each server can reach the other (key resolution + the join handshake).
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

            joined = await on_a.post(
                f"{_CS}/rooms/{room_id}/join", params={"server_name": "b.test"}, headers=headers
            )
            assert joined.status_code == 200, joined.text
            assert joined.json()["room_id"] == room_id

            # A now treats the room as local: it appears in joined_rooms,
            assert room_id in (
                await on_a.get(f"{_CS}/joined_rooms", headers=headers)
            ).json()["joined_rooms"]

            # both members are present (the remote creator and our user),
            members = (
                await on_a.get(f"{_CS}/rooms/{room_id}/joined_members", headers=headers)
            ).json()["joined"]
            assert "@alice:a.test" in members and "@bob:b.test" in members

            # and the adopted room state is readable.
            state = (
                await on_a.get(f"{_CS}/rooms/{room_id}/state", headers=headers)
            ).json()
            assert any(e["type"] == "m.room.create" for e in state)

        # B also sees our user joined.
        from neuron_server.storage import rooms as store

        b_members = await store.get_joined_members(app_b.state.db, room_id)  # type: ignore[attr-defined]
        assert "@alice:a.test" in b_members
