# SPDX-License-Identifier: Apache-2.0
"""Durable federation EDU queue.

Reliability-critical EDUs (``m.direct_to_device`` carrying Olm/Megolm key material,
``m.device_list_update``) are queued in ``federation_edu_outbox`` and survive an
offline peer — a dropped to-device message means "unable to decrypt". Ephemeral
EDUs (typing, receipts) stay best-effort and are never queued. Inbound
``m.direct_to_device`` is deduped on ``(origin, message_id)`` so durable retry
(which redelivers in a fresh transaction) applies an Olm message exactly once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.federation.sender import FederationSender
from neuron_server.storage import outbox as outbox_store
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.migrations import run_migrations

_DEST = "b.test"
_CS = "/_matrix/client/v3"


async def _edu_count(db: Database, destination: str) -> int:
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM federation_edu_outbox WHERE destination = ?", (destination,)
        )
    )


async def _pdu_count(db: Database, destination: str) -> int:
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM federation_outbox WHERE destination = ?", (destination,)
        )
    )


class _FakeClient:
    """A federation client that can be flipped offline, recording what it delivers."""

    def __init__(self) -> None:
        self.fail = False
        self.edus: list[dict[str, Any]] = []
        self.pdus: list[dict[str, Any]] = []

    async def put_json(self, destination: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise ConnectionError(f"{destination} is offline")
        self.edus.extend(body.get("edus", []))
        self.pdus.extend(body.get("pdus", []))
        return {}


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = connect_database("sqlite:///:memory:")
    await database.connect()
    await run_migrations(database)
    try:
        yield database
    finally:
        await database.disconnect()


def _sender(db: Database, client: _FakeClient) -> FederationSender:
    return FederationSender(db, "a.test", client)  # type: ignore[arg-type]


# --- (a) to-device survives an offline peer --------------------------------


async def test_to_device_is_queued_offline_then_delivered_on_retry(db: Database) -> None:
    client = _FakeClient()
    sender = _sender(db, client)

    client.fail = True
    await sender.send_direct_to_device(
        _DEST,
        sender="@alice:a.test",
        event_type="m.room.encrypted",
        message_id="m1",
        messages={"@bob:b.test": {"DEV": {"ciphertext": "opaque"}}},
    )
    # Not dropped: the EDU is durably queued, and delivery was attempted (failed).
    assert await _edu_count(db, _DEST) == 1
    assert client.edus == []

    # Peer recovers; retry drains the queued EDU.
    client.fail = False
    await sender.retry(_DEST)
    assert await _edu_count(db, _DEST) == 0
    assert [e["edu_type"] for e in client.edus] == ["m.direct_to_device"]
    assert client.edus[0]["content"]["message_id"] == "m1"


# --- (b) device_list_update survives an offline peer ------------------------


async def test_device_list_update_is_queued_offline_then_delivered(db: Database) -> None:
    # Alice (local) and Bob (remote) share a room, so b.test is a destination.
    for user in ("@alice:a.test", "@bob:b.test"):
        await db.execute(
            "INSERT INTO room_memberships (room_id, user_id, membership) VALUES (?, ?, 'join')",
            ("!r:a.test", user),
        )
    client = _FakeClient()
    sender = _sender(db, client)

    client.fail = True
    await sender.send_device_list_update("@alice:a.test", "DEV", stream_id=7)
    assert await _edu_count(db, _DEST) == 1  # queued, not dropped

    client.fail = False
    await sender.retry(_DEST)
    assert await _edu_count(db, _DEST) == 0
    assert [e["edu_type"] for e in client.edus] == ["m.device_list_update"]


# --- (c) typing / receipts are dropped, never queued ------------------------


async def test_transient_edus_are_dropped_offline_not_queued(db: Database) -> None:
    client = _FakeClient()
    sender = _sender(db, client)
    client.fail = True

    typing = {"edu_type": "m.typing", "content": {"room_id": "!r:a.test", "typing": True}}
    receipt = {"edu_type": "m.receipt", "content": {}}
    # Delivered transiently: dropped on failure, never persisted to either outbox.
    await sender._deliver(_DEST, new_pdus=[], transient_edus=[typing, receipt])

    assert await _edu_count(db, _DEST) == 0
    assert await _pdu_count(db, _DEST) == 0
    assert client.edus == []


async def test_transient_edu_sent_when_reachable(db: Database) -> None:
    client = _FakeClient()
    sender = _sender(db, client)
    typing = {"edu_type": "m.typing", "content": {"typing": False}}
    await sender._deliver(_DEST, new_pdus=[], transient_edus=[typing])
    assert [e["edu_type"] for e in client.edus] == ["m.typing"]
    assert await _edu_count(db, _DEST) == 0  # still never persisted


# --- (e) EDU outbox lease/release/delete + union ----------------------------


async def test_edu_outbox_lease_release_delete(db: Database) -> None:
    await outbox_store.enqueue_edu(db, _DEST, {"edu_type": "m.direct_to_device", "n": 1})
    await outbox_store.enqueue_edu(db, _DEST, {"edu_type": "m.direct_to_device", "n": 2})

    claimed = await outbox_store.claim_pending_edus(
        db, _DEST, "owner-a", now_ms=1000, lease_until_ms=61000
    )
    assert [e["n"] for _, e in claimed] == [1, 2]  # in order

    # A second worker before the lease expires claims nothing, and the destination
    # is not offered for draining while leased.
    assert await outbox_store.claim_pending_edus(
        db, _DEST, "owner-b", now_ms=2000, lease_until_ms=62000
    ) == []
    assert await outbox_store.destinations_with_pending(db, 2000) == []

    # Release hands them back for immediate reclaim.
    await outbox_store.release_edus(db, [sid for sid, _ in claimed], "owner-a")
    reclaimed = await outbox_store.claim_pending_edus(
        db, _DEST, "owner-c", now_ms=3000, lease_until_ms=63000
    )
    assert len(reclaimed) == 2
    # A stale owner can neither delete nor release.
    await outbox_store.delete_edus(db, [sid for sid, _ in reclaimed], "stale")
    assert await _edu_count(db, _DEST) == 2
    # The real owner deletes.
    await outbox_store.delete_edus(db, [sid for sid, _ in reclaimed], "owner-c")
    assert await _edu_count(db, _DEST) == 0


async def test_destinations_with_pending_unions_both_outboxes(db: Database) -> None:
    await outbox_store.enqueue(db, "pdu-only.test", {"type": "m.room.message"})
    await outbox_store.enqueue_edu(db, "edu-only.test", {"edu_type": "m.direct_to_device"})
    await outbox_store.enqueue(db, "both.test", {"type": "m.room.message"})
    await outbox_store.enqueue_edu(db, "both.test", {"edu_type": "m.direct_to_device"})

    dests = set(await outbox_store.destinations_with_pending(db, 10_000))
    assert dests == {"pdu-only.test", "edu-only.test", "both.test"}


# --- (f) PDUs and queued EDUs are delivered together, PDU semantics intact ---


async def test_deliver_sends_pdus_and_queued_edus_together(db: Database) -> None:
    await outbox_store.enqueue(db, _DEST, {"type": "m.room.message", "n": 1})
    await outbox_store.enqueue_edu(db, _DEST, {"edu_type": "m.direct_to_device", "n": 1})
    client = _FakeClient()
    sender = _sender(db, client)

    await sender._deliver(_DEST, new_pdus=[{"type": "m.room.message", "n": 2}], transient_edus=[])

    assert [p["n"] for p in client.pdus] == [1, 2]
    assert [e["n"] for e in client.edus] == [1]
    assert await _pdu_count(db, _DEST) == 0  # delivered rows deleted
    assert await _edu_count(db, _DEST) == 0


async def test_pdu_requeued_on_failure_edu_released(db: Database) -> None:
    """A failed send re-enqueues the unsent new PDU and releases the claimed EDU —
    PDU outbox semantics unchanged, and the EDU stays queued for the next retry."""
    await outbox_store.enqueue_edu(db, _DEST, {"edu_type": "m.direct_to_device", "n": 1})
    client = _FakeClient()
    sender = _sender(db, client)
    client.fail = True

    await sender._deliver(_DEST, new_pdus=[{"type": "m.room.message"}], transient_edus=[])

    # New PDU is now durably queued; the claimed EDU was released (still present).
    assert await _pdu_count(db, _DEST) == 1
    assert await _edu_count(db, _DEST) == 1
    # And both are claimable again (released, not stuck leased).
    assert set(await outbox_store.destinations_with_pending(db, 10_000)) == {_DEST}


# --- (d) inbound exactly-once dedup over federation -------------------------


class _TwoServers:
    def __init__(self, tmp_path: Path) -> None:
        self.app_a = create_app(
            NeuronServerSettings(name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}")
        )
        self.app_b = create_app(
            NeuronServerSettings(name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
        )

    def _opener(self, target: object):  # noqa: ANN202 - test helper
        def open_client(server_name: str) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=target), base_url=f"https://{server_name}"
            )

        return open_client

    async def __aenter__(self) -> _TwoServers:
        self._ctx_b = self.app_b.router.lifespan_context(self.app_b)
        self._ctx_a = self.app_a.router.lifespan_context(self.app_a)
        await self._ctx_b.__aenter__()
        await self._ctx_a.__aenter__()
        self.app_a.state.federation_client.open_client = self._opener(self.app_b)
        self.app_b.state.federation_client.open_client = self._opener(self.app_a)
        self.client_a = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app_a), base_url="https://a.test"
        )
        self.client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app_b), base_url="https://b.test"
        )
        self.alice = await _register(self.client_a, "alice")
        self.bob_h = {"Authorization": f"Bearer {await _register(self.client_b, 'bob')}"}
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client_a.aclose()
        await self.client_b.aclose()
        await self._ctx_a.__aexit__(None, None, None)
        await self._ctx_b.__aexit__(None, None, None)


async def _register(client: httpx.AsyncClient, username: str) -> str:
    session = (
        await client.post(
            f"{_CS}/register", json={"username": username, "password": "pw-123456"}
        )
    ).json()["session"]
    return (
        await client.post(
            f"{_CS}/register",
            json={
                "username": username,
                "password": "pw-123456",
                "auth": {"type": "m.login.dummy", "session": session},
            },
        )
    ).json()["access_token"]


async def test_inbound_to_device_deduped_across_transactions(tmp_path: Path) -> None:
    async with _TwoServers(tmp_path) as fed:
        # Deliver the SAME (sender, message_id) to-device EDU twice. Each
        # send_direct_to_device queues + delivers in a fresh transaction (new
        # txn_id), so transaction dedup can't catch the second — only the
        # message-level (origin, message_id) dedup does.
        for _ in range(2):
            await fed.app_a.state.federation_sender.send_direct_to_device(
                "b.test",
                sender="@alice:a.test",
                event_type="m.room.encrypted",
                message_id="dup-1",
                messages={"@bob:b.test": {"*": {"ciphertext": "opaque-olm-blob"}}},
            )

        events = (await fed.client_b.get(f"{_CS}/sync", headers=fed.bob_h)).json()[
            "to_device"
        ]["events"]
        # Applied exactly once despite two deliveries.
        assert len(events) == 1
        assert events[0]["type"] == "m.room.encrypted"
        # Both sends drained their queue (delivery succeeded HTTP-wise each time).
        assert await _edu_count(fed.app_a.state.db, "b.test") == 0
