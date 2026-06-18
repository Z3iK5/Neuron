# SPDX-License-Identifier: Apache-2.0
"""The federation send outbox: events queued for re-delivery to a destination that
was unreachable. Stream ids are assigned ``MAX+1`` for ordering and portability."""

from __future__ import annotations

import json
from typing import Any

from neuron_server.storage.database import Database


async def enqueue(db: Database, destination: str, pdu: dict[str, Any]) -> int:
    stream_id = int(
        await db.fetchval("SELECT COALESCE(MAX(stream_id), 0) + 1 FROM federation_outbox")
    )
    await db.execute(
        "INSERT INTO federation_outbox (stream_id, destination, pdu_json) VALUES (?, ?, ?)",
        (stream_id, destination, json.dumps(pdu)),
    )
    return stream_id


async def get_pending(db: Database, destination: str) -> list[tuple[int, dict[str, Any]]]:
    rows = await db.fetchall(
        "SELECT stream_id, pdu_json FROM federation_outbox"
        " WHERE destination = ? ORDER BY stream_id",
        (destination,),
    )
    return [(int(stream_id), json.loads(str(pdu_json))) for stream_id, pdu_json in rows]


async def delete(db: Database, stream_ids: list[int]) -> None:
    if not stream_ids:
        return
    async with db.transaction():
        for stream_id in stream_ids:
            await db.execute(
                "DELETE FROM federation_outbox WHERE stream_id = ?", (stream_id,)
            )


async def destinations_with_pending(db: Database) -> list[str]:
    rows = await db.fetchall("SELECT DISTINCT destination FROM federation_outbox")
    return [str(row[0]) for row in rows]
