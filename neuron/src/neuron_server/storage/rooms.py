# SPDX-License-Identifier: Apache-2.0
"""Data access for rooms, events, current state, memberships and txn dedupe."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from neuron_server.rooms.events import Event
from neuron_server.storage.database import Database


@dataclass
class RoomRow:
    room_id: str
    creator: str
    room_version: str
    created_ts: int


def _row_to_event(row: tuple[Any, ...]) -> Event:
    (
        event_id,
        room_id,
        etype,
        state_key,
        sender,
        content,
        origin_server_ts,
        depth,
        stream_ordering,
        unsigned,
        redacts,
    ) = row
    return Event(
        event_id=str(event_id),
        room_id=str(room_id),
        type=str(etype),
        sender=str(sender),
        content=json.loads(str(content)),
        origin_server_ts=int(origin_server_ts),
        depth=int(depth),
        stream_ordering=int(stream_ordering),
        state_key=None if state_key is None else str(state_key),
        unsigned=json.loads(str(unsigned)) if unsigned is not None else None,
        redacts=None if redacts is None else str(redacts),
    )


_EVENT_COLUMNS = (
    "event_id, room_id, type, state_key, sender, content, origin_server_ts,"
    " depth, stream_ordering, unsigned, redacts"
)


# --- rooms -----------------------------------------------------------------


async def create_room_row(
    db: Database, room_id: str, creator: str, room_version: str, created_ts: int
) -> None:
    await db.execute(
        "INSERT INTO rooms (room_id, creator, room_version, created_ts) VALUES (?, ?, ?, ?)",
        (room_id, creator, room_version, created_ts),
    )


async def get_room(db: Database, room_id: str) -> RoomRow | None:
    rows = await db.fetchall(
        "SELECT room_id, creator, room_version, created_ts FROM rooms WHERE room_id = ?",
        (room_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return RoomRow(str(row[0]), str(row[1]), str(row[2]), int(row[3]))


# --- events ----------------------------------------------------------------


async def next_stream_ordering(db: Database) -> int:
    value = await db.fetchval("SELECT COALESCE(MAX(stream_ordering), 0) + 1 FROM events")
    return int(value)


async def next_depth(db: Database, room_id: str) -> int:
    value = await db.fetchval(
        "SELECT COALESCE(MAX(depth), 0) + 1 FROM events WHERE room_id = ?", (room_id,)
    )
    return int(value)


async def get_max_stream_ordering(db: Database) -> int:
    value = await db.fetchval("SELECT COALESCE(MAX(stream_ordering), 0) FROM events")
    return int(value)


async def get_recent_events(db: Database, room_id: str, limit: int) -> list[Event]:
    """Return the most recent ``limit`` events in a room, ascending by ordering."""
    rows = await db.fetchall(
        f"SELECT {_EVENT_COLUMNS} FROM events WHERE room_id = ?"
        " ORDER BY stream_ordering DESC LIMIT ?",
        (room_id, limit),
    )
    return [_row_to_event(row) for row in reversed(rows)]


async def get_events_after(
    db: Database, room_id: str, after_ordering: int, limit: int
) -> list[Event]:
    """Return events with ordering greater than ``after_ordering`` (ascending)."""
    rows = await db.fetchall(
        f"SELECT {_EVENT_COLUMNS} FROM events"
        " WHERE room_id = ? AND stream_ordering > ?"
        " ORDER BY stream_ordering ASC LIMIT ?",
        (room_id, after_ordering, limit),
    )
    return [_row_to_event(row) for row in rows]


async def insert_event(db: Database, event: Event) -> None:
    await db.execute(
        "INSERT INTO events ("
        " event_id, room_id, type, state_key, sender, content, origin_server_ts,"
        " depth, stream_ordering, unsigned, redacts"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.event_id,
            event.room_id,
            event.type,
            event.state_key,
            event.sender,
            json.dumps(event.content),
            event.origin_server_ts,
            event.depth,
            event.stream_ordering,
            json.dumps(event.unsigned) if event.unsigned is not None else None,
            event.redacts,
        ),
    )


async def get_event(db: Database, room_id: str, event_id: str) -> Event | None:
    rows = await db.fetchall(
        f"SELECT {_EVENT_COLUMNS} FROM events WHERE room_id = ? AND event_id = ?",
        (room_id, event_id),
    )
    return _row_to_event(rows[0]) if rows else None


async def update_event_content(
    db: Database, event_id: str, content_json: str, unsigned_json: str | None
) -> None:
    await db.execute(
        "UPDATE events SET content = ?, unsigned = ? WHERE event_id = ?",
        (content_json, unsigned_json, event_id),
    )


async def get_messages(
    db: Database, room_id: str, *, from_ordering: int, direction: str, limit: int
) -> list[Event]:
    """Return up to ``limit`` events from ``from_ordering`` in ``direction`` ('b'/'f')."""
    if direction == "b":
        rows = await db.fetchall(
            f"SELECT {_EVENT_COLUMNS} FROM events"
            " WHERE room_id = ? AND stream_ordering < ?"
            " ORDER BY stream_ordering DESC LIMIT ?",
            (room_id, from_ordering, limit),
        )
    else:
        rows = await db.fetchall(
            f"SELECT {_EVENT_COLUMNS} FROM events"
            " WHERE room_id = ? AND stream_ordering > ?"
            " ORDER BY stream_ordering ASC LIMIT ?",
            (room_id, from_ordering, limit),
        )
    return [_row_to_event(row) for row in rows]


# --- current state ---------------------------------------------------------


async def update_current_state(
    db: Database, room_id: str, etype: str, state_key: str, event_id: str
) -> None:
    await db.execute(
        "INSERT INTO current_state (room_id, type, state_key, event_id)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(room_id, type, state_key) DO UPDATE SET event_id = excluded.event_id",
        (room_id, etype, state_key, event_id),
    )


async def get_current_state(db: Database, room_id: str) -> list[Event]:
    rows = await db.fetchall(
        f"SELECT {', '.join('e.' + c for c in _EVENT_COLUMNS.split(', '))}"
        " FROM current_state cs JOIN events e ON e.event_id = cs.event_id"
        " WHERE cs.room_id = ?",
        (room_id,),
    )
    return [_row_to_event(row) for row in rows]


async def get_state_event(
    db: Database, room_id: str, etype: str, state_key: str
) -> Event | None:
    rows = await db.fetchall(
        f"SELECT {', '.join('e.' + c for c in _EVENT_COLUMNS.split(', '))}"
        " FROM current_state cs JOIN events e ON e.event_id = cs.event_id"
        " WHERE cs.room_id = ? AND cs.type = ? AND cs.state_key = ?",
        (room_id, etype, state_key),
    )
    return _row_to_event(rows[0]) if rows else None


# --- memberships -----------------------------------------------------------


async def set_membership(db: Database, room_id: str, user_id: str, membership: str) -> None:
    await db.execute(
        "INSERT INTO room_memberships (room_id, user_id, membership)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(room_id, user_id) DO UPDATE SET membership = excluded.membership",
        (room_id, user_id, membership),
    )


async def get_joined_rooms(db: Database, user_id: str) -> list[str]:
    rows = await db.fetchall(
        "SELECT room_id FROM room_memberships WHERE user_id = ? AND membership = 'join'"
        " ORDER BY room_id",
        (user_id,),
    )
    return [str(row[0]) for row in rows]


async def get_user_memberships(db: Database, user_id: str) -> list[tuple[str, str]]:
    """Return ``(room_id, membership)`` for every room the user has a membership in."""
    rows = await db.fetchall(
        "SELECT room_id, membership FROM room_memberships WHERE user_id = ?",
        (user_id,),
    )
    return [(str(row[0]), str(row[1])) for row in rows]


async def count_rooms(db: Database) -> int:
    return int(await db.fetchval("SELECT COUNT(*) FROM rooms"))


async def list_rooms_page(
    db: Database, *, offset: int, limit: int
) -> list[RoomRow]:
    rows = await db.fetchall(
        "SELECT room_id, creator, room_version, created_ts FROM rooms"
        " ORDER BY room_id LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [RoomRow(str(r[0]), str(r[1]), str(r[2]), int(r[3])) for r in rows]


async def count_joined_members(db: Database, room_id: str) -> int:
    return int(
        await db.fetchval(
            "SELECT COUNT(*) FROM room_memberships WHERE room_id = ? AND membership = 'join'",
            (room_id,),
        )
    )


async def get_users_sharing_room(db: Database, user_id: str) -> list[str]:
    """Return all users who are joined to a room ``user_id`` is also joined to."""
    rows = await db.fetchall(
        "SELECT DISTINCT other.user_id FROM room_memberships me"
        " JOIN room_memberships other ON me.room_id = other.room_id"
        " WHERE me.user_id = ? AND me.membership = 'join' AND other.membership = 'join'",
        (user_id,),
    )
    return [str(row[0]) for row in rows]


async def get_joined_members(db: Database, room_id: str) -> list[str]:
    rows = await db.fetchall(
        "SELECT user_id FROM room_memberships WHERE room_id = ? AND membership = 'join'"
        " ORDER BY user_id",
        (room_id,),
    )
    return [str(row[0]) for row in rows]


# --- transaction de-duplication -------------------------------------------


async def get_txn_event(db: Database, user_id: str, txn_id: str) -> str | None:
    value = await db.fetchval(
        "SELECT event_id FROM event_txns WHERE user_id = ? AND txn_id = ?",
        (user_id, txn_id),
    )
    return None if value is None else str(value)


async def put_txn_event(db: Database, user_id: str, txn_id: str, event_id: str) -> None:
    await db.execute(
        "INSERT INTO event_txns (user_id, txn_id, event_id) VALUES (?, ?, ?)"
        " ON CONFLICT(user_id, txn_id) DO NOTHING",
        (user_id, txn_id, event_id),
    )
