# SPDX-License-Identifier: Apache-2.0
"""Federation discovery + remote key resolution (HS-7 step 4).

The headline test runs **two in-process homeservers**: server A resolves server
B's signing keys by fetching B's published key document over an ASGI transport,
then uses them to authenticate a federation request that B signed.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.federation.auth import sign_request
from neuron_server.federation.discovery import pick_base_url
from neuron_server.keys.resolver import parse_and_verify_key_document


def test_pick_base_url() -> None:
    # Explicit port wins and suppresses delegation.
    assert pick_base_url("hs.test:8449", {"m.server": "other:1"}) == "https://hs.test:8449"
    # Delegation is honoured, defaulting the port when absent.
    assert pick_base_url("hs.test", {"m.server": "delegated.example"}) == (
        "https://delegated.example:8448"
    )
    assert pick_base_url("hs.test", {"m.server": "delegated:443"}) == "https://delegated:443"
    # No delegation → default federation port.
    assert pick_base_url("hs.test", None) == "https://hs.test:8448"


def _doc(app: object) -> dict:
    return app.state.server_keys.server_key_document()  # type: ignore[attr-defined]


async def test_parse_and_verify_key_document(tmp_path: Path) -> None:
    app = create_app(
        NeuronServerSettings(name="hs.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
    )
    async with app.router.lifespan_context(app):
        doc = _doc(app)
        verified = parse_and_verify_key_document(doc, "hs.test")
        assert verified == {
            kid: v["key"] for kid, v in app.state.server_keys.verify_keys().items()
        }
        # Wrong server name is rejected.
        assert parse_and_verify_key_document(doc, "evil.test") is None
        # A tampered verify key breaks self-certification.
        forged = dict(doc)
        key_id = next(iter(forged["verify_keys"]))
        forged["verify_keys"] = {key_id: {"key": "AAAA" + forged["verify_keys"][key_id]["key"][4:]}}
        assert parse_and_verify_key_document(forged, "hs.test") is None


def _opener(app_b: object) -> object:
    def open_client(server_name: str) -> httpx.AsyncClient:
        if server_name == "b.test":
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
            )
        raise AssertionError(f"unexpected destination {server_name}")

    return open_client


async def test_two_server_key_resolution_and_request_auth(tmp_path: Path) -> None:
    app_a = create_app(
        NeuronServerSettings(name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}")
    )
    app_b = create_app(
        NeuronServerSettings(name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
    )

    async with app_b.router.lifespan_context(app_b), app_a.router.lifespan_context(app_a):
        app_a.state.federation_client.open_client = _opener(app_b)

        # A resolves B's keys by fetching B's key document over the ASGI transport.
        resolved = await app_a.state.server_key_resolver.verify_keys_for("b.test")
        assert resolved == {
            kid: v["key"] for kid, v in app_b.state.server_keys.verify_keys().items()
        }

        # The keys are now cached: resolution still works with the network removed.
        def _no_network(server_name: str) -> httpx.AsyncClient:
            raise AssertionError("network should not be used once keys are cached")

        app_a.state.federation_client.open_client = _no_network
        again = await app_a.state.server_key_resolver.verify_keys_for("b.test")
        assert again == resolved

        # End to end through the wired endpoint: B signs a request to A; A
        # authenticates B by the cached keys. Auth passes, so the failure is the
        # room-membership check (403), not an auth rejection (401).
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        ) as client_a:
            reg = await client_a.post(
                "/_matrix/client/v3/register", json={"username": "alice", "password": "pw-123456"}
            )
            session = reg.json()["session"]
            token = (
                await client_a.post(
                    "/_matrix/client/v3/register",
                    json={
                        "username": "alice",
                        "password": "pw-123456",
                        "auth": {"type": "m.login.dummy", "session": session},
                    },
                )
            ).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            room_id = (
                await client_a.post(
                    "/_matrix/client/v3/createRoom", headers=headers, json={}
                )
            ).json()["room_id"]
            event_id = (
                await client_a.put(
                    f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/t1",
                    headers=headers,
                    json={"msgtype": "m.text", "body": "hi"},
                )
            ).json()["event_id"]

            path = f"/_matrix/federation/v1/event/{event_id}"
            b_header = sign_request(
                method="GET", uri=path, origin="b.test", destination="a.test",
                signing_key=app_b.state.server_keys.signing_key,
            )
            authed = await client_a.get(path, headers={"Authorization": b_header})
            assert authed.status_code == 403
            assert "room" in authed.json()["error"].lower()

            # No auth header → 401.
            assert (await client_a.get(path)).status_code == 401

            # A signature by B over a different URI → 401 (auth failure).
            tampered = sign_request(
                method="GET", uri="/_matrix/federation/v1/event/$wrong", origin="b.test",
                destination="a.test", signing_key=app_b.state.server_keys.signing_key,
            )
            bad = await client_a.get(path, headers={"Authorization": tampered})
            assert bad.status_code == 401
