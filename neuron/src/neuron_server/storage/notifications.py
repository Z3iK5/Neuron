# SPDX-License-Identifier: Apache-2.0
"""Data access for recorded push notifications.

Every event a user's push rules decide to notify about is recorded here, so
``GET /_matrix/client/v3/notifications`` can list them newest-first with a
``read`` flag computed against the user's read receipt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from neuron_server.rooms.events import Event
from neuron_server.storage import rooms as rooms_store
from neuron_server.storage.database import Database


@dataclass(frozen=True)
class Notification:
    event: Event
    room_id: str
    actions: list[Any]
    ts: int
    highlight: bool


async def record(
    db: Database,
    user_id: str,
    *,
    event_id: str,
    room_id: str,
    actions: list[Any],
    ts: int,
    highlight: bool,
) -> None:
    """Record a notification for ``user_id`` (idempotent per (user, event))."""
    await db.execute(
        "INSERT INTO notifications (user_id, event_id, room_id, actions_json, ts, highlight)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, event_id) DO UPDATE SET"
        " actions_json = excluded.actions_json, ts = excluded.ts,"
        " highlight = excluded.highlight",
        (user_id, event_id, room_id, json.dumps(actions), ts, 1 if highlight else 0),
    )


async def _read_ceiling(db: Database, user_id: str) -> int:
    """The stream ordering of the latest event any of the user's read receipts
    (public or private, across all rooms) points at — the read/unread boundary."""
    value = await db.fetchval(
        "SELECT COALESCE(MAX(e.stream_ordering), 0) FROM receipts r"
        " JOIN events e ON e.room_id = r.room_id AND e.event_id = r.event_id"
        " WHERE r.user_id = ? AND r.receipt_type IN ('m.read', 'm.read.private')",
        (user_id,),
    )
    return int(value or 0)


async def list_for_user(
    db: Database,
    user_id: str,
    *,
    limit: int,
    from_ts: int | None,
    only_highlight: bool,
) -> tuple[list[tuple[Notification, bool]], int | None]:
    """Return ``([(notification, read), ...], next_from)`` newest-first.

    ``from_ts`` pages by the notification timestamp (exclusive upper bound);
    ``next_from`` is the ``from`` token for the next page, or ``None`` when this
    is the last page.
    """
    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    if only_highlight:
        where.append("highlight = 1")
    if from_ts is not None:
        where.append("ts < ?")
        params.append(from_ts)
    sql = (
        "SELECT event_id, room_id, actions_json, ts, highlight FROM notifications"
        f" WHERE {' AND '.join(where)} ORDER BY ts DESC, event_id DESC LIMIT ?"
    )
    params.append(limit + 1)
    rows = await db.fetchall(sql, params)

    ceiling = await _read_ceiling(db, user_id)
    out: list[tuple[Notification, bool]] = []
    for event_id, room_id, actions_json, ts, highlight in rows[:limit]:
        event = await rooms_store.get_event_global(db, str(event_id))
        if event is None:  # event was purged/redacted away; skip the stale row
            continue
        notification = Notification(
            event=event,
            room_id=str(room_id),
            actions=json.loads(str(actions_json)),
            ts=int(ts),
            highlight=bool(highlight),
        )
        out.append((notification, event.stream_ordering <= ceiling))
    next_from = int(rows[limit - 1][3]) if len(rows) > limit else None
    return out, next_from
