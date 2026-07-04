# SPDX-License-Identifier: Apache-2.0
"""Outbound federation retries (HS-7 step 6j).

A message sent while the destination server is unreachable is queued and delivered
on a later retry, so a transient outage doesn't silently drop events.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.storage.database import Database

_CS = "/_matrix/client/v3"


async def _outbox_count(db: Database, destination: str) -> int:
    """Rows queued for a destination (test-only peek at the outbox table)."""
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM federation_outbox WHERE destination = ?", (destination,)
        )
    )


def _opener(target_app: object):  # noqa: ANN202 - test helper
    def open_client(server_name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target_app), base_url=f"https://{server_name}"
        )

    return open_client


def _broken_opener(server_name: str) -> httpx.AsyncClient:
    raise ConnectionError(f"{server_name} is unreachable")


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


def _bodies(sync_json: dict, room_id: str) -> set[str]:
    room = sync_json["rooms"]["join"].get(room_id, {})
    events = room.get("timeline", {}).get("events", [])
    return {e["content"].get("body") for e in events if e["type"] == "m.room.message"}


async def test_message_is_queued_then_delivered_on_retry(tmp_path: Path) -> None:
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
            # A hosts a public room; Bob (on B) joins it.
            alice = await _register(client_a, "alice")
            alice_h = {"Authorization": f"Bearer {alice}"}
            room_id = (
                await client_a.post(
                    f"{_CS}/createRoom", headers=alice_h, json={"preset": "public_chat"}
                )
            ).json()["room_id"]
            bob = await _register(client_b, "bob")
            bob_h = {"Authorization": f"Bearer {bob}"}
            await client_b.post(
                f"{_CS}/rooms/{room_id}/join", params={"server_name": "a.test"}, headers=bob_h
            )

            # B goes offline; Alice's message can't be delivered and is queued.
            app_a.state.federation_client.open_client = _broken_opener
            await client_a.put(
                f"{_CS}/rooms/{room_id}/send/m.room.message/m1",
                headers=alice_h,
                json={"msgtype": "m.text", "body": "while offline"},
            )
            assert "while offline" not in _bodies(
                (await client_b.get(f"{_CS}/sync", headers=bob_h)).json(), room_id
            )
            assert await _outbox_count(app_a.state.db, "b.test") > 0  # queued

            # B comes back; retry flushes the backlog.
            app_a.state.federation_client.open_client = _opener(app_b)
            await app_a.state.federation_sender.retry("b.test")

            assert "while offline" in _bodies(
                (await client_b.get(f"{_CS}/sync", headers=bob_h)).json(), room_id
            )
            assert await _outbox_count(app_a.state.db, "b.test") == 0  # drained
        finally:
            await client_a.aclose()
            await client_b.aclose()
