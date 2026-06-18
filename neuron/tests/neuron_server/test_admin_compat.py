# SPDX-License-Identifier: Apache-2.0
"""HS-6 cut-over criterion: neuron_core's AdminClient drives neuron_server.

Runs the real ``neuron_core.AdminClient`` (which speaks the Synapse-compatible
``/_synapse/admin/...`` API) against an in-process ``neuron_server``, exercising
the operations the Neuron console and bots rely on. Also checks that a non-admin
token is refused.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from neuron_core import AdminClient
from neuron_core.errors import AdminApiError
from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"


async def _register(raw: httpx.AsyncClient, username: str) -> str:
    challenge = await raw.post(_REG, json={"username": username, "password": "pw-123456"})
    session = challenge.json()["session"]
    result = await raw.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    )
    return result.json()["access_token"]


async def test_admin_client_runs_against_neuron_server(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        admin_users="admin",
    )
    app = create_app(settings)
    base = "http://hs.test"

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base) as raw:
            admin_token = await _register(raw, "admin")
            alice_token = await _register(raw, "alice")
            room = (
                await raw.post(
                    "/_matrix/client/v3/createRoom",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"name": "Ops"},
                )
            ).json()["room_id"]

        admin_http = httpx.AsyncClient(
            transport=transport, base_url=base, headers={"Authorization": f"Bearer {admin_token}"}
        )
        admin = AdminClient(base, admin_token, client=admin_http)
        try:
            version = await admin.get_server_version()
            assert version["server_version"].startswith("Neuron")

            page = await admin.list_users(limit=50)
            names = {u["name"] for u in page.users}
            assert {"@admin:neuron.local", "@alice:neuron.local"} <= names

            assert (await admin.get_user("@alice:neuron.local"))["name"] == "@alice:neuron.local"

            # Create a user via the admin API.
            user, created = await admin.upsert_user(
                "@bob:neuron.local", password="pw-123456", displayname="Bob"
            )
            assert created is True
            assert (await admin.get_user("@bob:neuron.local"))["displayname"] == "Bob"

            await admin.deactivate_user("@bob:neuron.local")
            assert (await admin.get_user("@bob:neuron.local"))["deactivated"] is True

            await admin.reset_password("@alice:neuron.local", "another-password")

            token = await admin.create_registration_token(uses_allowed=5)
            listed = await admin.list_registration_tokens()
            assert any(t["token"] == token["token"] for t in listed)
            await admin.delete_registration_token(token["token"])

            rooms_page = await admin.list_rooms(limit=50)
            assert any(r["room_id"] == room for r in rooms_page.rooms)
            assert (await admin.get_room(room))["room_id"] == room
            assert "@admin:neuron.local" in await admin.get_room_members(room)
            assert any(e["type"] == "m.room.create" for e in await admin.get_room_state(room))

            await admin.make_room_admin(room, "@alice:neuron.local")
        finally:
            await admin_http.aclose()

        # A non-admin token is refused.
        alice_http = httpx.AsyncClient(
            transport=transport, base_url=base, headers={"Authorization": f"Bearer {alice_token}"}
        )
        alice_admin = AdminClient(base, alice_token, client=alice_http)
        try:
            with pytest.raises(AdminApiError) as excinfo:
                await alice_admin.list_users()
            assert excinfo.value.status_code == 403
        finally:
            await alice_http.aclose()
