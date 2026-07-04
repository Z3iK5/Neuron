# SPDX-License-Identifier: Apache-2.0
"""Tests for neuron_server E2EE relay endpoints (HS-5), without libolm.

Covers the relay contract: device-key upload/query, one-time-key counts/claim,
cross-signing upload/query, and sendToDevice delivery via /sync (with ack-based
cleanup). The full real-crypto pipeline is exercised in test_e2ee_pipeline.py.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.storage import e2ee as e2ee_store
from neuron_server.storage.database import connect_database
from neuron_server.storage.migrations import run_migrations

_REG = "/_matrix/client/v3/register"
_B = "/_matrix/client/v3"


def _client(tmp_path: Path) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    return TestClient(create_app(settings))


def _register(client: TestClient, username: str) -> tuple[str, str, str]:
    challenge = client.post(_REG, json={"username": username, "password": "pw-123456"})
    session = challenge.json()["session"]
    out = client.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    ).json()
    return out["access_token"], out["user_id"], out["device_id"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device_keys(user_id: str, device_id: str) -> dict[str, object]:
    return {
        "user_id": user_id,
        "device_id": device_id,
        "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
        "keys": {f"curve25519:{device_id}": "CURVE25519", f"ed25519:{device_id}": "ED25519"},
        "signatures": {},
    }


def test_keys_upload_query_and_counts(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id, device_id = _register(client, "alice")
        upload = client.post(
            f"{_B}/keys/upload",
            headers=_h(token),
            json={
                "device_keys": _device_keys(user_id, device_id),
                "one_time_keys": {
                    "signed_curve25519:AAAAAQ": {"key": "otk1"},
                    "signed_curve25519:AAAAAg": {"key": "otk2"},
                },
            },
        ).json()
        assert upload["one_time_key_counts"]["signed_curve25519"] == 2

        queried = client.post(
            f"{_B}/keys/query", headers=_h(token), json={"device_keys": {user_id: []}}
        ).json()
        device_obj = queried["device_keys"][user_id][device_id]
        assert device_obj["keys"][f"ed25519:{device_id}"] == "ED25519"


def test_one_time_key_claim_consumes_keys(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        a_token, a_user, a_device = _register(client, "alice")
        b_token, _b_user, _b_device = _register(client, "bob")
        client.post(
            f"{_B}/keys/upload",
            headers=_h(a_token),
            json={
                "device_keys": _device_keys(a_user, a_device),
                "one_time_keys": {"signed_curve25519:AAAAAQ": {"key": "only-one"}},
            },
        )

        claim = client.post(
            f"{_B}/keys/claim",
            headers=_h(b_token),
            json={"one_time_keys": {a_user: {a_device: "signed_curve25519"}}},
        ).json()
        claimed = claim["one_time_keys"][a_user][a_device]
        assert list(claimed.values())[0]["key"] == "only-one"

        # The key is consumed: count is now zero.
        counts = client.post(f"{_B}/keys/upload", headers=_h(a_token), json={}).json()
        assert counts["one_time_key_counts"].get("signed_curve25519", 0) == 0


async def test_claim_one_time_key_is_delete_authoritative(tmp_path: Path) -> None:
    """A key is returned only if this claim actually deleted its row (the pick and
    the delete are one DELETE ... RETURNING statement), so once it is consumed a
    later claim gets the reusable fallback key — never the same OTK twice."""
    db = connect_database(f"sqlite:///{tmp_path / 'hs.db'}")
    await db.connect()
    try:
        await run_migrations(db)
        user, device = "@alice:neuron.local", "DEV"
        await e2ee_store.store_one_time_keys(
            db, user, device, {"signed_curve25519:AAAAAQ": {"key": "otk"}}
        )
        await e2ee_store.store_fallback_keys(
            db, user, device, {"signed_curve25519:FB": {"key": "fb"}}
        )

        first = await e2ee_store.claim_one_time_key(db, user, device, "signed_curve25519")
        assert first == {"signed_curve25519:AAAAAQ": {"key": "otk"}}
        # The OTK row is gone; further claims fall back to the (unconsumed) fallback.
        second = await e2ee_store.claim_one_time_key(db, user, device, "signed_curve25519")
        assert second == {"signed_curve25519:FB": {"key": "fb"}}
        assert await e2ee_store.claim_one_time_key(db, user, device, "signed_curve25519") == second
    finally:
        await db.disconnect()


def test_cross_signing_upload_and_query(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, user_id, _device = _register(client, "alice")
        master = {
            "user_id": user_id,
            "usage": ["master"],
            "keys": {"ed25519:MASTER": "MASTERKEY"},
        }
        client.post(
            f"{_B}/keys/device_signing/upload", headers=_h(token), json={"master_key": master}
        )
        queried = client.post(
            f"{_B}/keys/query", headers=_h(token), json={"device_keys": {user_id: []}}
        ).json()
        assert queried["master_keys"][user_id]["keys"]["ed25519:MASTER"] == "MASTERKEY"


def test_send_to_device_delivers_via_sync_and_acks(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        a_token, a_user, _a_device = _register(client, "alice")
        b_token, b_user, _b_device = _register(client, "bob")

        client.put(
            f"{_B}/sendToDevice/m.room.encrypted/t1",
            headers=_h(a_token),
            json={"messages": {b_user: {"*": {"hello": "bob"}}}},
        )

        first = client.get(f"{_B}/sync?timeout=0", headers=_h(b_token)).json()
        events = first["to_device"]["events"]
        assert len(events) == 1
        assert events[0]["sender"] == a_user
        assert events[0]["type"] == "m.room.encrypted"
        assert events[0]["content"] == {"hello": "bob"}

        # Re-syncing with the returned token does not redeliver it.
        token = first["next_batch"]
        second = client.get(f"{_B}/sync?since={token}&timeout=0", headers=_h(b_token)).json()
        assert second["to_device"]["events"] == []
