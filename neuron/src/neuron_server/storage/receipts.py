# SPDX-License-Identifier: Apache-2.0
"""Storage for read receipts (local and received over federation).

Each receipt carries a ``stream_id`` (``MAX+1``) so ``/sync`` can report only the
rooms whose receipts changed since a client's token.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from neuron_server.storage.database import Database, escape_like


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


async def get_unread_counts(
    db: Database, room_id: str, user_id: str, highlight_terms: Sequence[str] = ()
) -> tuple[int, int]:
    """``(notification_count, highlight_count)`` for a user in a room.

    A family-scale approximation, one query per room: counts message-like events
    from *other* senders after the user's read point — the latest event covered by
    their ``m.read``/``m.read.private`` receipt, falling back to their current
    membership event (i.e. their join) when they have no receipt. Highlights are
    events whose content contains any ``highlight_terms`` needle (case-insensitive
    substring over the stored content JSON — good enough for name mentions).
    """
    like_clauses: list[str] = []
    params: list[Any] = []
    for term in highlight_terms:
        like_clauses.append("LOWER(content) LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(term.lower())}%")
    highlight_expr = (
        "SUM(CASE WHEN " + " OR ".join(like_clauses) + " THEN 1 ELSE 0 END)"
        if like_clauses
        else "0"
    )
    sql = (
        f"SELECT COUNT(*), COALESCE({highlight_expr}, 0) FROM events"
        " WHERE room_id = ? AND sender != ?"
        " AND type IN ('m.room.message', 'm.room.encrypted')"
        " AND stream_ordering > COALESCE("
        # The read point: the latest locally-known event any of the user's read
        # receipts (public or private) points at.
        " (SELECT MAX(re.stream_ordering) FROM receipts r"
        "   JOIN events re ON re.room_id = r.room_id AND re.event_id = r.event_id"
        "  WHERE r.room_id = ? AND r.user_id = ?"
        "  AND r.receipt_type IN ('m.read', 'm.read.private')),"
        # No receipt yet: count from the user's current membership (join) event.
        " (SELECT me.stream_ordering FROM current_state cs"
        "   JOIN events me ON me.event_id = cs.event_id"
        "  WHERE cs.room_id = ? AND cs.type = 'm.room.member' AND cs.state_key = ?),"
        " 0)"
    )
    params.extend((room_id, user_id, room_id, user_id, room_id, user_id))
    rows = await db.fetchall(sql, params)
    notifications, highlights = rows[0]
    return int(notifications), int(highlights or 0)
