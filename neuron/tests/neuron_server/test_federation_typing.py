# SPDX-License-Identifier: Apache-2.0
"""Typing notifications over federation (HS-7 step 6k)."""

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


def _typing_users(sync_json: dict, room_id: str) -> list[str]:
    room = sync_json["rooms"]["join"].get(room_id, {})
    for event in room.get("ephemeral", {}).get("events", []):
        if event["type"] == "m.typing":
            return event["content"].get("user_ids", [])
    return []


async def test_typing_propagates_over_federation(tmp_path: Path) -> None:
    app_a = create_app(
        NeuronServerSettings(name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}")
    )
    app_b = create_app(
        NeuronServerSettings(name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
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
            bob = await _register(client_b, "bob")
            bob_h = {"Authorization": f"Bearer {bob}"}
            room_id = (
                await client_b.post(
                    f"{_CS}/createRoom", headers=bob_h, json={"preset": "public_chat"}
                )
            ).json()["room_id"]
            alice = await _register(client_a, "alice")
            alice_h = {"Authorization": f"Bearer {alice}"}
            await client_a.post(
                f"{_CS}/rooms/{room_id}/join", params={"server_name": "b.test"}, headers=alice_h
            )

            # Alice starts typing → Bob sees it on B.
            await client_a.put(
                f"{_CS}/rooms/{room_id}/typing/@alice:a.test",
                headers=alice_h,
                json={"typing": True, "timeout": 30000},
            )
            assert "@alice:a.test" in _typing_users(
                (await client_b.get(f"{_CS}/sync", headers=bob_h)).json(), room_id
            )

            # Alice stops typing → she drops off Bob's view.
            await client_a.put(
                f"{_CS}/rooms/{room_id}/typing/@alice:a.test",
                headers=alice_h,
                json={"typing": False},
            )
            assert "@alice:a.test" not in _typing_users(
                (await client_b.get(f"{_CS}/sync", headers=bob_h)).json(), room_id
            )
        finally:
            await client_a.aclose()
            await client_b.aclose()
