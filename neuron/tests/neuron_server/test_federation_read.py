# SPDX-License-Identifier: Apache-2.0
"""Federation read API (``/_matrix/federation/v1/...``) — HS-7.

Drives the endpoints over loopback: signs requests with the server's own key
(the local-origin path), then verifies that the served PDUs carry valid
signatures — the same checks a remote homeserver would run.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.crypto.event_hashing import verify_event_signature
from neuron_server.federation.auth import sign_request

_NAME = "neuron.local"
_B = "/_matrix/client/v3"


def _app(tmp_path: Path) -> FastAPI:
    return create_app(
        NeuronServerSettings(name=_NAME, database_url=f"sqlite:///{tmp_path / 'hs.db'}")
    )


def _register(client: TestClient, username: str) -> str:
    session = client.post(_B + "/register", json={"username": username, "password": "pw-123456"})
    sess = session.json()["session"]
    out = client.post(
        _B + "/register",
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": sess},
        },
    )
    return out.json()["access_token"]


def _signed_get(client: TestClient, app: FastAPI, path: str) -> httpx.Response:
    header = sign_request(
        method="GET",
        uri=path,
        origin=_NAME,
        destination=_NAME,
        signing_key=app.state.server_keys.signing_key,
    )
    return client.get(path, headers={"Authorization": header})


def _server_verify_keys(app: FastAPI) -> dict[str, str]:
    return {kid: v["key"] for kid, v in app.state.server_keys.verify_keys().items()}


def test_federation_version_is_unauthenticated(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/_matrix/federation/v1/version").json()
    assert body["server"]["name"]


def test_event_endpoint_serves_verifiable_pdu(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        token = _register(client, "alice")
        headers = {"Authorization": f"Bearer {token}"}
        room_id = client.post(f"{_B}/createRoom", headers=headers, json={"name": "R"}).json()[
            "room_id"
        ]
        event_id = client.put(
            f"{_B}/rooms/{room_id}/send/m.room.message/t1",
            headers=headers,
            json={"msgtype": "m.text", "body": "hi"},
        ).json()["event_id"]

        path = f"/_matrix/federation/v1/event/{event_id}"

        # Unauthenticated → 401.
        assert client.get(path).status_code == 401

        # Properly signed → the PDU, with a signature a remote server can verify.
        resp = _signed_get(client, app, path)
        assert resp.status_code == 200
        pdus = resp.json()["pdus"]
        assert len(pdus) == 1
        pdu = pdus[0]
        assert "event_id" not in pdu  # v11 PDUs carry no event_id field
        key_id, verify_key = next(iter(_server_verify_keys(app).items()))
        assert verify_event_signature(
            pdu, server_name=_NAME, verify_key_base64=verify_key, key_id=key_id
        )

        # A signature over a different URI is rejected.
        bad = sign_request(
            method="GET", uri="/_matrix/federation/v1/event/$wrong", origin=_NAME,
            destination=_NAME, signing_key=app.state.server_keys.signing_key,
        )
        assert client.get(path, headers={"Authorization": bad}).status_code == 403


def test_state_endpoints(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        token = _register(client, "alice")
        headers = {"Authorization": f"Bearer {token}"}
        room_id = client.post(f"{_B}/createRoom", headers=headers, json={}).json()["room_id"]

        ids = _signed_get(client, app, f"/_matrix/federation/v1/state_ids/{room_id}").json()
        assert ids["pdu_ids"]  # current state present
        assert ids["auth_chain_ids"]  # auth chain present

        state = _signed_get(client, app, f"/_matrix/federation/v1/state/{room_id}").json()
        types = {pdu["type"] for pdu in state["pdus"]}
        assert "m.room.create" in types and "m.room.power_levels" in types
        # Every served state PDU is signed by us.
        key_id, verify_key = next(iter(_server_verify_keys(app).items()))
        for pdu in state["pdus"]:
            assert verify_event_signature(
                pdu, server_name=_NAME, verify_key_base64=verify_key, key_id=key_id
            )
