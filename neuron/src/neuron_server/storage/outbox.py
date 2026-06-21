# SPDX-License-Identifier: Apache-2.0
"""The federation send outbox: events queued for re-delivery to a destination that
was unreachable. Stream ids are assigned ``MAX+1`` for ordering and portability.

To stay correct with more than one worker, a destination's pending rows are
**leased** before a worker sends them (``owner`` + ``leased_until``): a concurrent
worker skips leased rows, so the same events are never sent twice; a crashed
worker's lease expires and another worker retries them.
"""

from __future__ import annotations

import json
from typing import Any

from neuron_server.storage.database import Database


async def enqueue(db: Database, destination: str, pdu: dict[str, Any]) -> int:
    stream_id = await db.next_stream_id("outbox")
    await db.execute(
        "INSERT INTO federation_outbox (stream_id, destination, pdu_json) VALUES (?, ?, ?)",
        (stream_id, destination, json.dumps(pdu)),
    )
    return stream_id


async def get_pending(db: Database, destination: str) -> list[tuple[int, dict[str, Any]]]:
    """All queued rows for a destination, in order (regardless of lease). Read-only;
    use :func:`claim_pending` to take ownership before sending."""
    rows = await db.fetchall(
        "SELECT stream_id, pdu_json FROM federation_outbox"
        " WHERE destination = ? ORDER BY stream_id",
        (destination,),
    )
    return [(int(stream_id), json.loads(str(pdu_json))) for stream_id, pdu_json in rows]


async def claim_pending(
    db: Database, destination: str, owner: str, *, now_ms: int, lease_until_ms: int
) -> list[tuple[int, dict[str, Any]]]:
    """Lease and return this destination's currently-unleased pending rows, in order.

    The lease (this ``owner`` + ``leased_until``) is taken in one transaction, so a
    concurrent worker leasing the same destination either blocks then sees the rows
    already leased (and claims nothing) or skips them — never a double-claim. Call
    :func:`delete` on success, or :func:`release` to hand them back on failure.
    """
    async with db.transaction():
        await db.execute(
            "UPDATE federation_outbox SET owner = ?, leased_until = ?"
            " WHERE destination = ? AND leased_until < ?",
            (owner, lease_until_ms, destination, now_ms),
        )
        rows = await db.fetchall(
            "SELECT stream_id, pdu_json FROM federation_outbox"
            " WHERE destination = ? AND owner = ? ORDER BY stream_id",
            (destination, owner),
        )
    return [(int(stream_id), json.loads(str(pdu_json))) for stream_id, pdu_json in rows]


async def release(db: Database, stream_ids: list[int], owner: str) -> None:
    """Hand leased rows back (after a failed send) so they're retried immediately.

    Scoped to ``owner``: if the lease already expired and another worker re-claimed
    the row, releasing it is a no-op (it isn't ours to hand back any more)."""
    if not stream_ids:
        return
    async with db.transaction():
        for stream_id in stream_ids:
            await db.execute(
                "UPDATE federation_outbox SET owner = NULL, leased_until = 0"
                " WHERE stream_id = ? AND owner = ?",
                (stream_id, owner),
            )


async def delete(db: Database, stream_ids: list[int], owner: str) -> None:
    """Remove delivered rows. Scoped to ``owner`` so an expired-then-reclaimed row
    (now owned by another worker mid-send) isn't deleted out from under it."""
    if not stream_ids:
        return
    async with db.transaction():
        for stream_id in stream_ids:
            await db.execute(
                "DELETE FROM federation_outbox WHERE stream_id = ? AND owner = ?",
                (stream_id, owner),
            )


async def destinations_with_pending(db: Database, now_ms: int) -> list[str]:
    """Destinations that have at least one currently-claimable (unleased) row."""
    rows = await db.fetchall(
        "SELECT DISTINCT destination FROM federation_outbox WHERE leased_until < ?",
        (now_ms,),
    )
    return [str(row[0]) for row in rows]
