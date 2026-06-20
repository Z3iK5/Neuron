# SPDX-License-Identifier: Apache-2.0
"""Storage for typing notifications (cross-process).

Typing is ephemeral, but it must be shared so a ``/sync`` served by any worker
sees a typing change made on another. One row per ``(room_id, user_id)`` ever
seen: setting (or clearing) typing **upserts** the row with a fresh ``stream_id``
(from the ``typing`` stream) and never deletes it, so ``MAX(stream_id)`` is a
monotonic serial that never regresses on "stop typing". A user is typing while
``expiry_ms`` is in the future; expired rows are simply filtered on read.
"""

from __future__ import annotations

from neuron_server.storage.database import Database


async def set_typing(db: Database, room_id: str, user_id: str, expiry_ms: int) -> int:
    """Record a user's typing state (``expiry_ms`` in the past clears it).

    Returns the new ``stream_id`` (the bumped serial).
    """
    stream_id = await db.next_stream_id("typing")
    await db.execute(
        "INSERT INTO typing (room_id, user_id, expiry_ms, stream_id)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(room_id, user_id) DO UPDATE SET"
        " expiry_ms = excluded.expiry_ms,"
        # Never move a row's stream_id backwards: under concurrent Postgres
        # connections the sequence (nextval) allocation order can differ from
        # commit order, so the last writer may carry a lower id. A portable
        # CASE-max keeps MAX(stream_id) monotonic (the serial /sync compares).
        " stream_id = CASE WHEN excluded.stream_id > typing.stream_id"
        " THEN excluded.stream_id ELSE typing.stream_id END",
        (room_id, user_id, expiry_ms, stream_id),
    )
    return stream_id


async def is_typing(db: Database, room_id: str, user_id: str, now_ms: int) -> bool:
    """Whether the user is currently (non-expired) typing in the room."""
    row = await db.fetchval(
        "SELECT 1 FROM typing WHERE room_id = ? AND user_id = ? AND expiry_ms > ?",
        (room_id, user_id, now_ms),
    )
    return row is not None


async def typing_users(db: Database, room_id: str, now_ms: int) -> list[str]:
    """The users currently typing in a room (expired entries excluded), sorted."""
    rows = await db.fetchall(
        "SELECT user_id FROM typing WHERE room_id = ? AND expiry_ms > ? ORDER BY user_id",
        (room_id, now_ms),
    )
    return [str(user_id) for (user_id,) in rows]


async def max_typing_stream(db: Database) -> int:
    """The current typing serial (0 when nobody has ever typed)."""
    return int(await db.fetchval("SELECT COALESCE(MAX(stream_id), 0) FROM typing"))
