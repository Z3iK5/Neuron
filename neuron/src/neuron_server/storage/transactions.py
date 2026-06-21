# SPDX-License-Identifier: Apache-2.0
"""Inbound federation transaction dedup.

Records ``(origin, txn_id)`` for transactions we've processed so a retry — which
a remote server sends if it didn't receive our response, and which may land on a
different worker — can be short-circuited instead of re-processed.
"""

from __future__ import annotations

from neuron_server.storage.database import Database


async def was_received(db: Database, origin: str, txn_id: str) -> bool:
    """Whether this ``(origin, txn_id)`` transaction has already been processed."""
    row = await db.fetchval(
        "SELECT 1 FROM received_transactions WHERE origin = ? AND txn_id = ?",
        (origin, txn_id),
    )
    return row is not None


async def mark_received(db: Database, origin: str, txn_id: str, ts: int) -> None:
    """Record a processed transaction (idempotent on a concurrent double-process)."""
    await db.execute(
        "INSERT INTO received_transactions (origin, txn_id, received_ts)"
        " VALUES (?, ?, ?) ON CONFLICT (origin, txn_id) DO NOTHING",
        (origin, txn_id, ts),
    )
