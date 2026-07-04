# SPDX-License-Identifier: Apache-2.0
"""Storage for read receipts (local and received over federation).

Each receipt carries a ``stream_id`` (``MAX+1``) so ``/sync`` can report only the
rooms whose receipts changed since a client's token.
"""

from __future__ import annotations

from dataclasses import dataclass

from neuron_server.storage.database import Database


@dataclass(frozen=True)
class Receipt:
    user_id: str
    receipt_type: str
    event_id: str
    ts: int
    stream_id: int


async def upsert_receipt(
    db: Database, room_id: str, user_id: str, receipt_type: str, event_id: str, ts: int
) -> int:
    """Record (or move forward) a user's receipt; returns the new stream id.

    The id allocation and insert run in one transaction so the multi-writer
    position tracker counts the id as in-flight until the row commits (callers are
    not already inside a transaction).
    """
    async with db.transaction():
        stream_id = await db.next_stream_id("receipts")
        await db.execute(
            "INSERT INTO receipts (room_id, user_id, receipt_type, event_id, ts, stream_id)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(room_id, user_id, receipt_type) DO UPDATE SET"
            " event_id = excluded.event_id, ts = excluded.ts, stream_id = excluded.stream_id",
            (room_id, user_id, receipt_type, event_id, ts, stream_id),
        )
    return stream_id


async def get_room_receipts(db: Database, room_id: str) -> list[Receipt]:
    rows = await db.fetchall(
        "SELECT user_id, receipt_type, event_id, ts, stream_id FROM receipts WHERE room_id = ?",
        (room_id,),
    )
    return [
        Receipt(
            user_id=str(user_id),
            receipt_type=str(receipt_type),
            event_id=str(event_id),
            ts=int(ts),
            stream_id=int(stream_id),
        )
        for user_id, receipt_type, event_id, ts, stream_id in rows
    ]
