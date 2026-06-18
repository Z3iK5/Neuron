# SPDX-License-Identifier: Apache-2.0
"""Federation backfill (HS-7 step 6h).

A user joins a room that already has history; joining triggers a backfill, so the
prior messages show up in the new member's ``/sync``.
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


async def test_join_backfills_prior_history(tmp_path: Path) -> None:
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
            bob_h = {"Authorization": f"Bearer {bob}"}
            room_id = (
                await on_b.post(
                    f"{_CS}/createRoom", headers=bob_h, json={"preset": "public_chat"}
                )
            ).json()["room_id"]
            # History created *before* Alice joins.
            for i in range(3):
                await on_b.put(
                    f"{_CS}/rooms/{room_id}/send/m.room.message/h{i}",
                    headers=bob_h,
                    json={"msgtype": "m.text", "body": f"history-{i}"},
                )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        ) as on_a:
            alice = await _register(on_a, "alice")
            alice_h = {"Authorization": f"Bearer {alice}"}
            joined = await on_a.post(
                f"{_CS}/rooms/{room_id}/join", params={"server_name": "b.test"}, headers=alice_h
            )
            assert joined.status_code == 200

            sync = (await on_a.get(f"{_CS}/sync", headers=alice_h)).json()
            timeline = sync["rooms"]["join"][room_id]["timeline"]["events"]
            bodies = {e["content"].get("body") for e in timeline if e["type"] == "m.room.message"}
            assert {"history-0", "history-1", "history-2"} <= bodies
