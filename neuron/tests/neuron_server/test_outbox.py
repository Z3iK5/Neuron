# SPDX-License-Identifier: Apache-2.0
"""Tests for the leased federation outbox (single-owner draining)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from neuron_server.storage import outbox as outbox_store
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.migrations import run_migrations

_DEST = "b.test"


async def _pending_count(db: Database, destination: str) -> int:
    """Rows queued for a destination regardless of lease (test-only peek)."""
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM federation_outbox WHERE destination = ?", (destination,)
        )
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = connect_database("sqlite:///:memory:")
    await database.connect()
    await run_migrations(database)
    try:
        yield database
    finally:
        await database.disconnect()


async def test_claim_leases_rows_exclusively(db: Database) -> None:
    await outbox_store.enqueue(db, _DEST, {"n": 1})
    await outbox_store.enqueue(db, _DEST, {"n": 2})

    claimed = await outbox_store.claim_pending(
        db, _DEST, "owner-a", now_ms=1000, lease_until_ms=61000
    )
    assert [pdu["n"] for _, pdu in claimed] == [1, 2]  # in order

    # A second worker claiming before the lease ends / a delete gets nothing.
    claimed_b = await outbox_store.claim_pending(
        db, _DEST, "owner-b", now_ms=2000, lease_until_ms=62000
    )
    assert claimed_b == []
    # And the destination is no longer offered for draining while leased.
    assert await outbox_store.destinations_with_pending(db, 2000) == []


async def test_delete_removes_claimed_rows(db: Database) -> None:
    await outbox_store.enqueue(db, _DEST, {"n": 1})
    claimed = await outbox_store.claim_pending(
        db, _DEST, "o", now_ms=1000, lease_until_ms=61000
    )
    await outbox_store.delete(db, [sid for sid, _ in claimed], "o")
    assert await _pending_count(db, _DEST) == 0


async def test_delete_release_are_owner_scoped(db: Database) -> None:
    await outbox_store.enqueue(db, _DEST, {"n": 1})
    claimed = await outbox_store.claim_pending(
        db, _DEST, "owner-a", now_ms=1000, lease_until_ms=61000
    )
    ids = [sid for sid, _ in claimed]
    # A stale owner can neither delete nor release a row it no longer holds.
    await outbox_store.delete(db, ids, "stale-owner")
    await outbox_store.release(db, ids, "stale-owner")
    assert await _pending_count(db, _DEST) == 1  # untouched


async def test_release_allows_immediate_reclaim(db: Database) -> None:
    await outbox_store.enqueue(db, _DEST, {"n": 1})
    claimed = await outbox_store.claim_pending(
        db, _DEST, "o1", now_ms=1000, lease_until_ms=61000
    )
    await outbox_store.release(db, [sid for sid, _ in claimed], "o1")
    # Released rows are claimable again right away (a failed send retries promptly).
    reclaimed = await outbox_store.claim_pending(
        db, _DEST, "o2", now_ms=2000, lease_until_ms=62000
    )
    assert len(reclaimed) == 1


async def test_expired_lease_is_reclaimable(db: Database) -> None:
    await outbox_store.enqueue(db, _DEST, {"n": 1})
    await outbox_store.claim_pending(db, _DEST, "o1", now_ms=1000, lease_until_ms=2000)
    # Past the lease expiry the row is offered again (a crashed worker's backlog).
    assert await outbox_store.destinations_with_pending(db, 5000) == [_DEST]
    reclaimed = await outbox_store.claim_pending(
        db, _DEST, "o2", now_ms=5000, lease_until_ms=65000
    )
    assert len(reclaimed) == 1
