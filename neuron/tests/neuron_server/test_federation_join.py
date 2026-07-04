# SPDX-License-Identifier: Apache-2.0
"""Federated join — resident side (HS-7 step 6a).

A user on server A joins a public room hosted by server B via make_join/send_join.
The test plays the joining server (A): it fetches a join template from B, completes
and signs the join event with A's key, and sends it back. Afterwards B's room shows
the remote user as joined.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.crypto.event_hashing import add_hashes_and_signatures, compute_event_id
from neuron_server.federation.auth import sign_request
from neuron_server.storage import rooms as store

_ALICE = "@alice:a.test"


def _opener(target_app: object):  # noqa: ANN202 - test helper
    def open_client(server_name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target_app), base_url=f"https://{server_name}"
        )

    return open_client


async def _create_public_room_on_b(app_b: object) -> str:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
    ) as client:
        reg = await client.post(
            "/_matrix/client/v3/register", json={"username": "bob", "password": "pw-123456"}
        )
        session = reg.json()["session"]
        token = (
            await client.post(
                "/_matrix/client/v3/register",
                json={
                    "username": "bob",
                    "password": "pw-123456",
                    "auth": {"type": "m.login.dummy", "session": session},
                },
            )
        ).json()["access_token"]
        room_id = (
            await client.post(
                "/_matrix/client/v3/createRoom",
                headers={"Authorization": f"Bearer {token}"},
                json={"preset": "public_chat"},
            )
        ).json()["room_id"]
    return room_id


async def test_remote_user_joins_our_room(tmp_path: Path) -> None:
    app_a = create_app(
        NeuronServerSettings(name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}")
    )
    app_b = create_app(
        NeuronServerSettings(name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
    )
    a_key = None

    async with app_b.router.lifespan_context(app_b), app_a.router.lifespan_context(app_a):
        # B must be able to fetch A's keys to authenticate A and validate A's join event.
        app_b.state.federation_client.open_client = _opener(app_a)
        a_key = app_a.state.server_keys.signing_key

        room_id = await _create_public_room_on_b(app_b)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
        ) as as_a:
            # 1) make_join: fetch the join-event template from B (signed as A).
            make_path = f"/_matrix/federation/v1/make_join/{room_id}/{_ALICE}"
            make_header = sign_request(
                method="GET", uri=make_path, origin="a.test", destination="b.test",
                signing_key=a_key,
            )
            make_resp = await as_a.get(make_path, headers={"Authorization": make_header})
            assert make_resp.status_code == 200, make_resp.text
            template = make_resp.json()["event"]
            assert template["sender"] == _ALICE and template["content"]["membership"] == "join"

            # 2) Complete & sign the join event with A's key.
            join_event = add_hashes_and_signatures(
                template, server_name="a.test", signing_key=a_key
            )
            event_id = compute_event_id(join_event)

            # 3) send_join: hand the signed join back to B.
            send_path = f"/_matrix/federation/v2/send_join/{room_id}/{event_id}"
            send_header = sign_request(
                method="PUT", uri=send_path, origin="a.test", destination="b.test",
                signing_key=a_key, content=join_event,
            )
            send_resp = await as_a.put(
                send_path, json=join_event, headers={"Authorization": send_header}
            )
            assert send_resp.status_code == 200, send_resp.text
            payload = send_resp.json()
            state_types = {e["type"] for e in payload["state"]}
            assert "m.room.create" in state_types
            assert payload["auth_chain"]

            # 3b) A retried send_join (same signed event) is idempotent: it must
            # return the room state again, not 500 on the duplicate insert.
            retry = await as_a.put(
                send_path, json=join_event, headers={"Authorization": send_header}
            )
            assert retry.status_code == 200, retry.text
            assert "m.room.create" in {e["type"] for e in retry.json()["state"]}

            # 4) Make-join for a user not on the origin server is refused.
            bad_path = f"/_matrix/federation/v1/make_join/{room_id}/@x:other.test"
            bad_header = sign_request(
                method="GET", uri=bad_path, origin="a.test", destination="b.test",
                signing_key=a_key,
            )
            bad = await as_a.get(bad_path, headers={"Authorization": bad_header})
            assert bad.status_code == 403

        # B's room now lists the remote user as joined.
        members = await store.get_joined_members(app_b.state.db, room_id)  # type: ignore[attr-defined]
        assert _ALICE in members
