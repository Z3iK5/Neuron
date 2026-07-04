# SPDX-License-Identifier: Apache-2.0
"""Storage for invites received over federation to rooms we don't host.

Each invite carries a ``stream_id`` (assigned ``MAX+1`` for portability) so
``/sync`` can tell which invites are new since a client's token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from neuron_server.storage.database import Database


@dataclass(frozen=True)
class PendingInvite:
    room_id: str
    event: dict[str, Any]
    invite_state: list[dict[str, Any]]
    stream_id: int


async def store_invite(
    db: Database,
    user_id: str,
    room_id: str,
    event: dict[str, Any],
    invite_state: list[dict[str, Any]],
) -> int:
    """Record (or refresh) an invite; returns its new stream id.

    The id allocation and insert run in one transaction so the multi-writer
    position tracker counts the id as in-flight until the row commits (callers are
    not already inside a transaction).
    """
    async with db.transaction():
        stream_id = await db.next_stream_id("federated_invites")
        await db.execute(
            "INSERT INTO federated_invites"
            " (user_id, room_id, event_json, invite_state_json, stream_id)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, room_id) DO UPDATE SET"
            " event_json = excluded.event_json, invite_state_json = excluded.invite_state_json,"
            " stream_id = excluded.stream_id",
            (user_id, room_id, json.dumps(event), json.dumps(invite_state), stream_id),
        )
    return stream_id


async def list_pending_invites(db: Database, user_id: str) -> list[PendingInvite]:
    rows = await db.fetchall(
        "SELECT room_id, event_json, invite_state_json, stream_id"
        " FROM federated_invites WHERE user_id = ? ORDER BY stream_id",
        (user_id,),
    )
    return [
        PendingInvite(
            room_id=str(room_id),
            event=json.loads(str(event_json)),
            invite_state=json.loads(str(invite_state_json)),
            stream_id=int(stream_id),
        )
        for room_id, event_json, invite_state_json, stream_id in rows
    ]


async def delete_invite(db: Database, user_id: str, room_id: str) -> None:
    await db.execute(
        "DELETE FROM federated_invites WHERE user_id = ? AND room_id = ?", (user_id, room_id)
    )
