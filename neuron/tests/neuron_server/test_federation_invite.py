# SPDX-License-Identifier: Apache-2.0
"""Federated invite (HS-7 step 6e).

Server A hosts an invite-only room and invites a user on server B. B validates and
co-signs the invite and records it; A applies it. The decisive end-to-end check is
that the invited remote user can then **join the invite-only room over
federation** — which only succeeds because the invite was applied.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.storage import invites as invite_store
from neuron_server.storage import rooms as store

_CS = "/_matrix/client/v3"
_BOB = "@bob:b.test"


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


async def test_invite_remote_user_then_they_join(tmp_path: Path) -> None:
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
            await _register(on_b, "bob")  # bob exists on B

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        ) as on_a:
            alice = await _register(on_a, "alice")
            headers = {"Authorization": f"Bearer {alice}"}
            # An invite-only room (default private preset → join_rule "invite").
            room_id = (
                await on_a.post(f"{_CS}/createRoom", headers=headers, json={})
            ).json()["room_id"]

            invited = await on_a.post(
                f"{_CS}/rooms/{room_id}/invite", headers=headers, json={"user_id": _BOB}
            )
            assert invited.status_code == 200, invited.text

        # B recorded the invite, co-signed by both servers.
        pending = await invite_store.list_pending_invites(app_b.state.db, _BOB)  # type: ignore[attr-defined]
        recorded = next((p for p in pending if p.room_id == room_id), None)
        assert recorded is not None
        assert {"a.test", "b.test"} <= set(recorded.event["signatures"])

        # A's room state shows bob invited (not yet joined).
        assert _BOB not in await store.get_joined_members(app_a.state.db, room_id)  # type: ignore[attr-defined]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
        ) as on_b:
            bob_token = (
                await on_b.post(
                    f"{_CS}/login",
                    json={
                        "type": "m.login.password",
                        "identifier": {"type": "m.id.user", "user": "bob"},
                        "password": "pw-123456",
                    },
                )
            ).json()["access_token"]
            bob_headers = {"Authorization": f"Bearer {bob_token}"}

            # Bob's /sync on B surfaces the federated invite.
            sync = (await on_b.get(f"{_CS}/sync", headers=bob_headers)).json()
            assert room_id in sync["rooms"]["invite"]
            events = sync["rooms"]["invite"][room_id]["invite_state"]["events"]
            assert any(
                e["type"] == "m.room.member"
                and e["state_key"] == _BOB
                and e["content"]["membership"] == "invite"
                for e in events
            )

            # The invited user joins the invite-only room over federation.
            joined = await on_b.post(
                f"{_CS}/rooms/{room_id}/join",
                params={"server_name": "a.test"},
                headers=bob_headers,
            )
            assert joined.status_code == 200, joined.text

            # After joining, the room moves from invite to join in Bob's /sync.
            sync2 = (await on_b.get(f"{_CS}/sync", headers=bob_headers)).json()
            assert room_id not in sync2["rooms"]["invite"]
            assert room_id in sync2["rooms"]["join"]

        # Bob is now joined on A (the resident server).
        assert _BOB in await store.get_joined_members(app_a.state.db, room_id)  # type: ignore[attr-defined]
