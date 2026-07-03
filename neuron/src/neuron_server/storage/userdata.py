# SPDX-License-Identifier: Apache-2.0
"""Data access for per-user data: profiles, account data, and filters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from neuron_server.storage.database import Database

# --- profiles --------------------------------------------------------------


async def get_profile(db: Database, user_id: str) -> dict[str, Any]:
    rows = await db.fetchall(
        "SELECT displayname, avatar_url FROM profiles WHERE user_id = ?", (user_id,)
    )
    if not rows:
        return {}
    profile: dict[str, Any] = {}
    if rows[0][0] is not None:
        profile["displayname"] = str(rows[0][0])
    if rows[0][1] is not None:
        profile["avatar_url"] = str(rows[0][1])
    return profile


async def set_displayname(db: Database, user_id: str, displayname: str | None) -> None:
    await db.execute(
        "INSERT INTO profiles (user_id, displayname) VALUES (?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET displayname = excluded.displayname",
        (user_id, displayname),
    )


async def set_avatar_url(db: Database, user_id: str, avatar_url: str | None) -> None:
    await db.execute(
        "INSERT INTO profiles (user_id, avatar_url) VALUES (?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET avatar_url = excluded.avatar_url",
        (user_id, avatar_url),
    )


# --- account data ----------------------------------------------------------


async def get_account_data(
    db: Database, user_id: str, room_id: str, data_type: str
) -> dict[str, Any] | None:
    value = await db.fetchval(
        "SELECT content_json FROM account_data WHERE user_id = ? AND room_id = ? AND type = ?",
        (user_id, room_id, data_type),
    )
    return None if value is None else json.loads(str(value))


async def set_account_data(
    db: Database, user_id: str, room_id: str, data_type: str, content: dict[str, Any]
) -> int:
    """Upsert one account-data entry; returns its new stream id.

    Allocation and insert share a transaction (like receipts) so the multi-writer
    position tracker counts the id as in-flight until the row commits.
    """
    async with db.transaction():
        stream_id = await db.next_stream_id("account_data")
        await db.execute(
            "INSERT INTO account_data (user_id, room_id, type, content_json, stream_id)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, room_id, type) DO UPDATE SET"
            " content_json = excluded.content_json, stream_id = excluded.stream_id",
            (user_id, room_id, data_type, json.dumps(content), stream_id),
        )
    return stream_id


@dataclass(frozen=True)
class AccountDataEntry:
    room_id: str  # "" for global account data
    data_type: str
    content: dict[str, Any]
    stream_id: int


async def get_account_data_changes(
    db: Database, user_id: str, since_stream: int
) -> list[AccountDataEntry]:
    """All of a user's account-data entries changed after ``since_stream``.

    Pass ``-1`` for an initial sync so pre-migration rows (stream_id 0) are
    included too.
    """
    rows = await db.fetchall(
        "SELECT room_id, type, content_json, stream_id FROM account_data"
        " WHERE user_id = ? AND stream_id > ? ORDER BY stream_id",
        (user_id, since_stream),
    )
    return [
        AccountDataEntry(
            room_id=str(room_id),
            data_type=str(data_type),
            content=json.loads(str(content_json)),
            stream_id=int(stream_id),
        )
        for room_id, data_type, content_json, stream_id in rows
    ]


# --- filters ---------------------------------------------------------------


async def create_filter(
    db: Database, user_id: str, filter_id: str, definition: dict[str, Any]
) -> None:
    await db.execute(
        "INSERT INTO filters (user_id, filter_id, definition_json) VALUES (?, ?, ?)",
        (user_id, filter_id, json.dumps(definition)),
    )


async def get_filter(db: Database, user_id: str, filter_id: str) -> dict[str, Any] | None:
    value = await db.fetchval(
        "SELECT definition_json FROM filters WHERE user_id = ? AND filter_id = ?",
        (user_id, filter_id),
    )
    return None if value is None else json.loads(str(value))


async def count_filters(db: Database, user_id: str) -> int:
    return int(
        await db.fetchval("SELECT COUNT(*) FROM filters WHERE user_id = ?", (user_id,))
    )
