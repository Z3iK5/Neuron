# SPDX-License-Identifier: Apache-2.0
"""HS-1 done-criterion: neuron_core's own client authenticates against neuron_server.

Drives the real ``neuron_core.MatrixClient`` against an in-process ``neuron_server``
app (via httpx's ASGI transport, with the app's lifespan running so the DB is
connected and migrated). Registers a user through the server, then calls
``whoami`` with the issued token.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from neuron_core import MatrixClient
from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_CREATE = "/_matrix/client/v3/createRoom"


async def test_matrixclient_whoami_against_neuron_server(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
    )
    app = create_app(settings)
    base = "http://hs.test"

    # Run the app's lifespan so storage is connected/migrated and auth is wired.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)

        # Register through the server to obtain an access token.
        async with httpx.AsyncClient(transport=transport, base_url=base) as raw:
            challenge = await raw.post(_REG, json={"username": "alice", "password": "pw-123456"})
            assert challenge.status_code == 401
            session = challenge.json()["session"]
            registered = await raw.post(
                _REG,
                json={
                    "username": "alice",
                    "password": "pw-123456",
                    "auth": {"type": "m.login.dummy", "session": session},
                },
            )
            assert registered.status_code == 200
            token = registered.json()["access_token"]

        # Now use neuron_core's own client, pointed at the same app.
        core_client = httpx.AsyncClient(
            transport=transport, base_url=base, headers={"Authorization": f"Bearer {token}"}
        )
        matrix = MatrixClient(base, token, client=core_client)
        try:
            who = await matrix.whoami()
        finally:
            await core_client.aclose()

        assert who["user_id"] == "@alice:neuron.local"
        assert who["device_id"]


async def test_matrixclient_joined_rooms_against_neuron_server(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
    )
    app = create_app(settings)
    base = "http://hs.test"

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url=base) as raw:
            challenge = await raw.post(_REG, json={"username": "alice", "password": "pw-123456"})
            session = challenge.json()["session"]
            registered = await raw.post(
                _REG,
                json={
                    "username": "alice",
                    "password": "pw-123456",
                    "auth": {"type": "m.login.dummy", "session": session},
                },
            )
            token = registered.json()["access_token"]
            created = await raw.post(
                _CREATE, headers={"Authorization": f"Bearer {token}"}, json={}
            )
            room_id = created.json()["room_id"]

        # neuron_core's MatrixClient reads the room list via GET /v3/joined_rooms.
        core_client = httpx.AsyncClient(
            transport=transport, base_url=base, headers={"Authorization": f"Bearer {token}"}
        )
        matrix = MatrixClient(base, token, client=core_client)
        try:
            rooms = await matrix.joined_rooms()
        finally:
            await core_client.aclose()

        assert room_id in rooms
