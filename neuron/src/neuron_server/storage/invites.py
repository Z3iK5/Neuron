# SPDX-License-Identifier: Apache-2.0
"""Storage for invites received over federation to rooms we don't host."""

from __future__ import annotations

import json
from typing import Any

from neuron_server.storage.database import Database


async def store_invite(
    db: Database,
    user_id: str,
    room_id: str,
    event: dict[str, Any],
    invite_state: list[dict[str, Any]],
) -> None:
    await db.execute(
        "INSERT INTO federated_invites (user_id, room_id, event_json, invite_state_json)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(user_id, room_id) DO UPDATE SET"
        " event_json = excluded.event_json, invite_state_json = excluded.invite_state_json",
        (user_id, room_id, json.dumps(event), json.dumps(invite_state)),
    )


async def get_invite(db: Database, user_id: str, room_id: str) -> dict[str, Any] | None:
    rows = await db.fetchall(
        "SELECT event_json, invite_state_json FROM federated_invites"
        " WHERE user_id = ? AND room_id = ?",
        (user_id, room_id),
    )
    if not rows:
        return None
    return {
        "event": json.loads(str(rows[0][0])),
        "invite_state": json.loads(str(rows[0][1])),
    }
