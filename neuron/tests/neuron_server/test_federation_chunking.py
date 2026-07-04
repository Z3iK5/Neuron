# SPDX-License-Identifier: Apache-2.0
"""Outbound transaction chunking: a backlog larger than the spec's per-transaction
limits (50 PDUs / 100 EDUs) is split into sequential transactions in stream order,
and a failed batch stops the send with the remainder kept queued for retry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio

from neuron_server.federation.sender import FederationSender
from neuron_server.storage import outbox as outbox_store
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.migrations import run_migrations

_DEST = "b.test"


class _RecordingClient:
    """Stands in for FederationClient; records transactions, optionally failing."""

    def __init__(self, fail_at_txn: int | None = None) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.fail_at_txn = fail_at_txn

    async def put_json(self, destination: str, path: str, body: dict[str, Any]) -> dict:
        if self.fail_at_txn is not None and len(self.transactions) + 1 == self.fail_at_txn:
            raise ConnectionError("destination went away")
        self.transactions.append(body)
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


async def _queued(db: Database) -> list[int]:
    rows = await db.fetchall(
        "SELECT pdu_json FROM federation_outbox WHERE destination = ? ORDER BY stream_id",
        (_DEST,),
    )
    import json

    return [json.loads(str(r[0]))["n"] for r in rows]


async def test_backlog_is_chunked_at_50_pdus_in_order(db: Database) -> None:
    client = _RecordingClient()
    sender = FederationSender(db, "a.test", client)  # type: ignore[arg-type]
    for n in range(120):
        await outbox_store.enqueue(db, _DEST, {"n": n})

    await sender.retry(_DEST)

    assert [len(t["pdus"]) for t in client.transactions] == [50, 50, 20]
    sent = [pdu["n"] for t in client.transactions for pdu in t["pdus"]]
    assert sent == list(range(120))  # global order preserved across batches
    assert await _queued(db) == []  # drained


async def test_exactly_50_pdus_is_a_single_transaction(db: Database) -> None:
    client = _RecordingClient()
    sender = FederationSender(db, "a.test", client)  # type: ignore[arg-type]
    for n in range(50):
        await outbox_store.enqueue(db, _DEST, {"n": n})

    await sender.retry(_DEST)

    assert [len(t["pdus"]) for t in client.transactions] == [50]


async def test_failed_batch_stops_send_and_keeps_remainder_queued(db: Database) -> None:
    client = _RecordingClient(fail_at_txn=2)
    sender = FederationSender(db, "a.test", client)  # type: ignore[arg-type]
    for n in range(120):
        await outbox_store.enqueue(db, _DEST, {"n": n})

    await sender.retry(_DEST)

    # Only the first batch went out; nothing after the failure was attempted.
    assert [len(t["pdus"]) for t in client.transactions] == [50]
    # The delivered 50 are gone; the remaining 70 stay queued in order and are
    # immediately claimable again (the lease was handed back).
    assert await _queued(db) == list(range(50, 120))
    assert await outbox_store.destinations_with_pending(db, 10**15) == [_DEST]

    # A later retry delivers the rest, still in order.
    client.fail_at_txn = None
    await sender.retry(_DEST)
    sent = [pdu["n"] for t in client.transactions for pdu in t["pdus"]]
    assert sent == list(range(120))
    assert await _queued(db) == []


async def test_new_pdus_behind_failed_backlog_are_queued(db: Database) -> None:
    """A new event that can't be sent (backlog batch failed first) is queued
    behind the released backlog, preserving per-destination ordering."""
    client = _RecordingClient(fail_at_txn=1)
    sender = FederationSender(db, "a.test", client)  # type: ignore[arg-type]
    for n in range(3):
        await outbox_store.enqueue(db, _DEST, {"n": n})

    await sender._deliver(_DEST, new_pdus=[{"n": 3}], edus=[])  # noqa: SLF001

    assert client.transactions == []
    assert await _queued(db) == [0, 1, 2, 3]

    client.fail_at_txn = None
    await sender.retry(_DEST)
    assert [pdu["n"] for t in client.transactions for pdu in t["pdus"]] == [0, 1, 2, 3]


async def test_edus_are_chunked_at_100(db: Database) -> None:
    client = _RecordingClient()
    sender = FederationSender(db, "a.test", client)  # type: ignore[arg-type]
    edus = [{"edu_type": "m.typing", "content": {"n": n}} for n in range(250)]

    await sender._deliver(_DEST, new_pdus=[], edus=edus)  # noqa: SLF001

    assert [len(t["edus"]) for t in client.transactions] == [100, 100, 50]
