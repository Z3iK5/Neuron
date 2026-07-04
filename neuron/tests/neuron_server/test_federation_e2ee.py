# SPDX-License-Identifier: Apache-2.0
"""E2EE over federation.

Two in-process homeservers (routed to each other over ASGI transports): device
keys are queryable across servers, one-time keys are claimable (and consumed),
to-device messages arrive as ``m.direct_to_device`` EDUs, and device-list changes
propagate as ``m.device_list_update`` EDUs into ``/sync`` ``device_lists.changed``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


async def _register(client: httpx.AsyncClient, username: str) -> tuple[str, str]:
    """Register ``username``; return (access_token, device_id)."""
    session = (
        await client.post(
            f"{_CS}/register", json={"username": username, "password": "pw-123456"}
        )
    ).json()["session"]
    out = (
        await client.post(
            f"{_CS}/register",
            json={
                "username": username,
                "password": "pw-123456",
                "auth": {"type": "m.login.dummy", "session": session},
            },
        )
    ).json()
    return out["access_token"], out["device_id"]


def _device_keys(user_id: str, device_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "device_id": device_id,
        "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
        "keys": {
            f"curve25519:{device_id}": "curve-pub",
            f"ed25519:{device_id}": "ed-pub",
        },
        "signatures": {user_id: {f"ed25519:{device_id}": "sig"}},
    }


class _TwoServers:
    """Two federated homeservers with one registered user on each."""

    def __init__(self, tmp_path: Path) -> None:
        self.app_a = create_app(
            NeuronServerSettings(name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}")
        )
        self.app_b = create_app(
            NeuronServerSettings(name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
        )

    async def __aenter__(self) -> _TwoServers:
        self._ctx_b = self.app_b.router.lifespan_context(self.app_b)
        self._ctx_a = self.app_a.router.lifespan_context(self.app_a)
        await self._ctx_b.__aenter__()
        await self._ctx_a.__aenter__()
        self.app_a.state.federation_client.open_client = _opener(self.app_b)
        self.app_b.state.federation_client.open_client = _opener(self.app_a)
        self.client_a = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app_a), base_url="https://a.test"
        )
        self.client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app_b), base_url="https://b.test"
        )
        alice_token, self.alice_device = await _register(self.client_a, "alice")
        bob_token, self.bob_device = await _register(self.client_b, "bob")
        self.alice_h = {"Authorization": f"Bearer {alice_token}"}
        self.bob_h = {"Authorization": f"Bearer {bob_token}"}
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client_a.aclose()
        await self.client_b.aclose()
        await self._ctx_a.__aexit__(None, None, None)
        await self._ctx_b.__aexit__(None, None, None)

    async def join_shared_room(self) -> str:
        """Bob creates a room on B; Alice joins from A. Returns the room id."""
        room_id = (
            await self.client_b.post(
                f"{_CS}/createRoom", headers=self.bob_h, json={"preset": "public_chat"}
            )
        ).json()["room_id"]
        joined = await self.client_a.post(
            f"{_CS}/rooms/{room_id}/join",
            params={"server_name": "b.test"},
            headers=self.alice_h,
        )
        assert joined.status_code == 200
        return room_id


async def test_keys_query_across_federation(tmp_path: Path) -> None:
    async with _TwoServers(tmp_path) as fed:
        # Bob uploads device keys and cross-signing keys on B.
        upload = await fed.client_b.post(
            f"{_CS}/keys/upload",
            headers=fed.bob_h,
            json={"device_keys": _device_keys("@bob:b.test", fed.bob_device)},
        )
        assert upload.status_code == 200
        master = {
            "user_id": "@bob:b.test",
            "usage": ["master"],
            "keys": {"ed25519:masterpub": "masterpub"},
        }
        self_signing = {
            "user_id": "@bob:b.test",
            "usage": ["self_signing"],
            "keys": {"ed25519:sskpub": "sskpub"},
        }
        await fed.client_b.post(
            f"{_CS}/keys/device_signing/upload",
            headers=fed.bob_h,
            json={"master_key": master, "self_signing_key": self_signing},
        )

        # Alice queries Bob's keys from A — served over federation.
        result = (
            await fed.client_a.post(
                f"{_CS}/keys/query",
                headers=fed.alice_h,
                json={"device_keys": {"@bob:b.test": []}},
            )
        ).json()
        assert result["failures"] == {}
        keys = result["device_keys"]["@bob:b.test"][fed.bob_device]
        assert keys["keys"][f"ed25519:{fed.bob_device}"] == "ed-pub"
        assert result["master_keys"]["@bob:b.test"] == master
        assert result["self_signing_keys"]["@bob:b.test"] == self_signing

        # A second query is served from the in-process cache (same result).
        again = (
            await fed.client_a.post(
                f"{_CS}/keys/query",
                headers=fed.alice_h,
                json={"device_keys": {"@bob:b.test": []}},
            )
        ).json()
        assert again["device_keys"] == result["device_keys"]


async def test_keys_query_unreachable_server_reports_failure(tmp_path: Path) -> None:
    async with _TwoServers(tmp_path) as fed:
        # Route c.test to a transport that refuses to connect.
        def _refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable", request=request)

        real_opener = fed.app_a.state.federation_client.open_client

        def open_client(server_name: str) -> httpx.AsyncClient:
            if server_name == "c.test":
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(_refuse), base_url="https://c.test"
                )
            return real_opener(server_name)

        fed.app_a.state.federation_client.open_client = open_client

        result = await fed.client_a.post(
            f"{_CS}/keys/query",
            headers=fed.alice_h,
            json={"device_keys": {"@carol:c.test": []}},
        )
        assert result.status_code == 200
        body = result.json()
        assert body["failures"] == {"c.test": {}}
        assert body["device_keys"] == {}


async def test_keys_claim_across_federation_consumes_the_otk(tmp_path: Path) -> None:
    async with _TwoServers(tmp_path) as fed:
        await fed.client_b.post(
            f"{_CS}/keys/upload",
            headers=fed.bob_h,
            json={
                "one_time_keys": {
                    "signed_curve25519:AAAAAA": {"key": "otk-pub", "signatures": {}}
                }
            },
        )

        claim_body = {
            "one_time_keys": {"@bob:b.test": {fed.bob_device: "signed_curve25519"}}
        }
        claimed = (
            await fed.client_a.post(f"{_CS}/keys/claim", headers=fed.alice_h, json=claim_body)
        ).json()
        assert claimed["failures"] == {}
        assert claimed["one_time_keys"]["@bob:b.test"][fed.bob_device] == {
            "signed_curve25519:AAAAAA": {"key": "otk-pub", "signatures": {}}
        }

        # The OTK was consumed on B: a second claim finds nothing (no fallback).
        again = (
            await fed.client_a.post(f"{_CS}/keys/claim", headers=fed.alice_h, json=claim_body)
        ).json()
        assert again["one_time_keys"] == {}


async def test_send_to_device_across_federation(tmp_path: Path) -> None:
    async with _TwoServers(tmp_path) as fed:
        sent = await fed.client_a.put(
            f"{_CS}/sendToDevice/m.room.encrypted/txn1",
            headers=fed.alice_h,
            json={
                "messages": {
                    "@bob:b.test": {fed.bob_device: {"ciphertext": "opaque-olm-blob"}}
                }
            },
        )
        assert sent.status_code == 200

        bob_sync = (await fed.client_b.get(f"{_CS}/sync", headers=fed.bob_h)).json()
        events = bob_sync["to_device"]["events"]
        assert events == [
            {
                "sender": "@alice:a.test",
                "type": "m.room.encrypted",
                "content": {"ciphertext": "opaque-olm-blob"},
            }
        ]


async def test_send_to_device_wildcard_reaches_all_remote_devices(tmp_path: Path) -> None:
    async with _TwoServers(tmp_path) as fed:
        await fed.client_a.put(
            f"{_CS}/sendToDevice/m.room_key_request/txn2",
            headers=fed.alice_h,
            json={"messages": {"@bob:b.test": {"*": {"action": "request"}}}},
        )
        bob_sync = (await fed.client_b.get(f"{_CS}/sync", headers=fed.bob_h)).json()
        types = [e["type"] for e in bob_sync["to_device"]["events"]]
        assert types == ["m.room_key_request"]


async def test_device_list_update_reaches_remote_sync(tmp_path: Path) -> None:
    async with _TwoServers(tmp_path) as fed:
        await fed.join_shared_room()

        # Bob establishes a sync position first, so the change is incremental.
        since = (await fed.client_b.get(f"{_CS}/sync", headers=fed.bob_h)).json()["next_batch"]

        # Alice uploads device keys on A -> m.device_list_update EDU to B.
        await fed.client_a.post(
            f"{_CS}/keys/upload",
            headers=fed.alice_h,
            json={"device_keys": _device_keys("@alice:a.test", fed.alice_device)},
        )

        bob_sync = (
            await fed.client_b.get(
                f"{_CS}/sync", headers=fed.bob_h, params={"since": since, "timeout": 0}
            )
        ).json()
        assert "@alice:a.test" in bob_sync["device_lists"]["changed"]


async def test_local_device_deletion_emits_update(tmp_path: Path) -> None:
    async with _TwoServers(tmp_path) as fed:
        await fed.join_shared_room()
        since = (await fed.client_b.get(f"{_CS}/sync", headers=fed.bob_h)).json()["next_batch"]

        # Alice logs out, which deletes her device on A.
        out = await fed.client_a.post(f"{_CS}/logout", headers=fed.alice_h, json={})
        assert out.status_code == 200

        bob_sync = (
            await fed.client_b.get(
                f"{_CS}/sync", headers=fed.bob_h, params={"since": since, "timeout": 0}
            )
        ).json()
        assert "@alice:a.test" in bob_sync["device_lists"]["changed"]


async def test_inbound_user_devices_endpoint(tmp_path: Path) -> None:
    async with _TwoServers(tmp_path) as fed:
        await fed.client_b.post(
            f"{_CS}/keys/upload",
            headers=fed.bob_h,
            json={"device_keys": _device_keys("@bob:b.test", fed.bob_device)},
        )
        # A asks B for Bob's device list over the signed federation endpoint.
        result = await fed.app_a.state.federation_client.get_json(
            "b.test", "/_matrix/federation/v1/user/devices/@bob:b.test"
        )
        assert result["user_id"] == "@bob:b.test"
        assert isinstance(result["stream_id"], int)
        assert [d["device_id"] for d in result["devices"]] == [fed.bob_device]
        assert result["devices"][0]["keys"]["device_id"] == fed.bob_device
