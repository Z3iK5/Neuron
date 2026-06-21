# SPDX-License-Identifier: Apache-2.0
"""SQLite get_stream_position behaviour (the degenerate, MAX-based floor).

The multi-writer (MIN-across-instances) machinery is Postgres-only and is covered
in ``tests/integration/test_postgres.py``; on SQLite a single serialized
connection means allocation order == commit order, so the floor is just MAX(col).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from neuron_server.storage import invites as invites_store
from neuron_server.storage import receipts as receipts_store
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.migrations import run_migrations


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = connect_database("sqlite:///:memory:")
    await database.connect()
    await run_migrations(database)
    await database.ensure_stream_sequences()
    try:
        yield database
    finally:
        await database.disconnect()


async def test_get_stream_position_is_max_and_starts_at_zero(db: Database) -> None:
    assert await db.get_stream_position("receipts") == 0
    # upsert_receipt now wraps allocation+insert in a transaction — on SQLite this
    # must not deadlock and the floor must track MAX(stream_id).
    await receipts_store.upsert_receipt(db, "!r:n", "@a:n", "m.read", "$e1", 1)
    assert await db.get_stream_position("receipts") == 1
    await receipts_store.upsert_receipt(db, "!r:n", "@b:n", "m.read", "$e2", 2)
    assert await db.get_stream_position("receipts") == 2
    direct = int(await db.fetchval("SELECT COALESCE(MAX(stream_id), 0) FROM receipts"))
    assert await db.get_stream_position("receipts") == direct


async def test_store_invite_in_transaction_tracks_position(db: Database) -> None:
    assert await db.get_stream_position("federated_invites") == 0
    await invites_store.store_invite(db, "@a:n", "!room:other", {"type": "m.room.member"}, [])
    assert await db.get_stream_position("federated_invites") == 1
