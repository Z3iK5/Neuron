# SPDX-License-Identifier: Apache-2.0
"""Two-server transaction ingest (HS-7 step 5).

Server B builds a genuine signed event and sends it to server A in a federation
transaction; A authenticates B and validates the PDU (resolving B's keys), then a
tampered transaction is rejected with a per-PDU error.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.federation.auth import sign_request
from neuron_server.storage import rooms as store

_SEND = "/_matrix/federation/v1/send/txn1"


def _opener(app_b: object):  # noqa: ANN202 - test helper
    def open_client(server_name: str) -> httpx.AsyncClient:
        if server_name == "b.test":
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
            )
        raise AssertionError(f"unexpected destination {server_name}")

    return open_client


async def _make_event_on_b(app_b: object) -> dict:
    """Register a user on B, create a room, send a message; return the event PDU."""
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
        headers = {"Authorization": f"Bearer {token}"}
        room_id = (
            await client.post("/_matrix/client/v3/createRoom", headers=headers, json={})
        ).json()["room_id"]
        event_id = (
            await client.put(
                f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/t1",
                headers=headers,
                json={"msgtype": "m.text", "body": "federated hello"},
            )
        ).json()["event_id"]
    event = await store.get_event_global(app_b.state.db, event_id)  # type: ignore[attr-defined]
    assert event is not None
    return event.pdu_dict()


def _transaction(pdu: dict) -> dict:
    return {"origin": "b.test", "origin_server_ts": int(time.time() * 1000), "pdus": [pdu]}


def _sign_put(app_b: object, body: dict) -> str:
    return sign_request(
        method="PUT",
        uri=_SEND,
        origin="b.test",
        destination="a.test",
        signing_key=app_b.state.server_keys.signing_key,  # type: ignore[attr-defined]
        content=body,
    )


async def test_transaction_validates_real_pdu_and_rejects_tampering(tmp_path: Path) -> None:
    app_a = create_app(
        NeuronServerSettings(name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}")
    )
    app_b = create_app(
        NeuronServerSettings(name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
    )

    async with app_b.router.lifespan_context(app_b), app_a.router.lifespan_context(app_a):
        app_a.state.federation_client.open_client = _opener(app_b)
        pdu = await _make_event_on_b(app_b)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        ) as client_a:
            # A genuine, signed PDU is accepted (empty per-PDU result).
            body = _transaction(pdu)
            resp = await client_a.put(
                _SEND, json=body, headers={"Authorization": _sign_put(app_b, body)}
            )
            assert resp.status_code == 200
            results = resp.json()["pdus"]
            assert results == {pdu_id: {} for pdu_id in results}
            assert len(results) == 1

            # Tampering the event content breaks its content hash → per-PDU error.
            tampered_pdu = dict(pdu)
            tampered_pdu["content"] = {"msgtype": "m.text", "body": "evil"}
            tbody = _transaction(tampered_pdu)
            tresp = await client_a.put(
                _SEND, json=tbody, headers={"Authorization": _sign_put(app_b, tbody)}
            )
            assert tresp.status_code == 200
            (result,) = tresp.json()["pdus"].values()
            assert "content hash" in result["error"]

            # An unauthenticated transaction is refused outright.
            assert (await client_a.put(_SEND, json=body)).status_code == 401
