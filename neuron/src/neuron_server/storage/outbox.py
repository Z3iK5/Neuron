# SPDX-License-Identifier: Apache-2.0
"""The federation send outboxes: PDUs and reliability-critical EDUs queued for
re-delivery to a destination that was unreachable. Stream ids are assigned
``MAX+1`` for ordering and portability.

Two tables share one lease model:

* ``federation_outbox`` (PDUs) — room events.
* ``federation_edu_outbox`` (EDUs) — ``m.direct_to_device`` (Olm/Megolm key
  material) and ``m.device_list_update``. Ephemeral EDUs (typing, receipts) are
  point-in-time and are **never** queued here — queuing a stale one is wrong.

To stay correct with more than one worker, a destination's pending rows are
**leased** before a worker sends them (``owner`` + ``leased_until``): a concurrent
worker skips leased rows, so the same units are never sent twice; a crashed
worker's lease expires and another worker retries them. The PDU and EDU functions
are thin wrappers over the shared ``_enqueue``/``_claim``/``_release``/``_delete``
helpers, differing only in the table/json-column/stream they target.
"""

from __future__ import annotations

import json
from typing import Any

from neuron_server.storage.database import Database

# (table, json column, stream name) for each queue.
_PDU = ("federation_outbox", "pdu_json", "outbox")
_EDU = ("federation_edu_outbox", "edu_json", "edu_outbox")


async def _enqueue(
    db: Database, table: str, col: str, stream: str, destination: str, payload: dict[str, Any]
) -> int:
    stream_id = await db.next_stream_id(stream)
    await db.execute(
        f"INSERT INTO {table} (stream_id, destination, {col}) VALUES (?, ?, ?)",
        (stream_id, destination, json.dumps(payload)),
    )
    return stream_id


async def _claim(
    db: Database,
    table: str,
    col: str,
    destination: str,
    owner: str,
    *,
    now_ms: int,
    lease_until_ms: int,
) -> list[tuple[int, dict[str, Any]]]:
    """Lease and return this destination's currently-unleased pending rows, in order.

    The lease (this ``owner`` + ``leased_until``) is taken in one transaction, so a
    concurrent worker leasing the same destination either blocks then sees the rows
    already leased (and claims nothing) or skips them — never a double-claim. Call
    :func:`_delete` on success, or :func:`_release` to hand them back on failure.
    """
    async with db.transaction():
        await db.execute(
            f"UPDATE {table} SET owner = ?, leased_until = ?"
            " WHERE destination = ? AND leased_until < ?",
            (owner, lease_until_ms, destination, now_ms),
        )
        rows = await db.fetchall(
            f"SELECT stream_id, {col} FROM {table}"
            " WHERE destination = ? AND owner = ? ORDER BY stream_id",
            (destination, owner),
        )
    return [(int(stream_id), json.loads(str(payload))) for stream_id, payload in rows]


async def _release(db: Database, table: str, stream_ids: list[int], owner: str) -> None:
    """Hand leased rows back (after a failed send) so they're retried immediately.

    Scoped to ``owner``: if the lease already expired and another worker re-claimed
    the row, releasing it is a no-op (it isn't ours to hand back any more)."""
    if not stream_ids:
        return
    async with db.transaction():
        for stream_id in stream_ids:
            await db.execute(
                f"UPDATE {table} SET owner = NULL, leased_until = 0"
                " WHERE stream_id = ? AND owner = ?",
                (stream_id, owner),
            )


async def _delete(db: Database, table: str, stream_ids: list[int], owner: str) -> None:
    """Remove delivered rows. Scoped to ``owner`` so an expired-then-reclaimed row
    (now owned by another worker mid-send) isn't deleted out from under it."""
    if not stream_ids:
        return
    async with db.transaction():
        for stream_id in stream_ids:
            await db.execute(
                f"DELETE FROM {table} WHERE stream_id = ? AND owner = ?",
                (stream_id, owner),
            )


# --- PDU outbox --------------------------------------------------------------


async def enqueue(db: Database, destination: str, pdu: dict[str, Any]) -> int:
    return await _enqueue(db, *_PDU, destination, pdu)


async def claim_pending(
    db: Database, destination: str, owner: str, *, now_ms: int, lease_until_ms: int
) -> list[tuple[int, dict[str, Any]]]:
    table, col, _ = _PDU
    return await _claim(
        db, table, col, destination, owner, now_ms=now_ms, lease_until_ms=lease_until_ms
    )


async def release(db: Database, stream_ids: list[int], owner: str) -> None:
    await _release(db, _PDU[0], stream_ids, owner)


async def delete(db: Database, stream_ids: list[int], owner: str) -> None:
    await _delete(db, _PDU[0], stream_ids, owner)


# --- EDU outbox --------------------------------------------------------------


async def enqueue_edu(db: Database, destination: str, edu: dict[str, Any]) -> int:
    return await _enqueue(db, *_EDU, destination, edu)


async def claim_pending_edus(
    db: Database, destination: str, owner: str, *, now_ms: int, lease_until_ms: int
) -> list[tuple[int, dict[str, Any]]]:
    table, col, _ = _EDU
    return await _claim(
        db, table, col, destination, owner, now_ms=now_ms, lease_until_ms=lease_until_ms
    )


async def release_edus(db: Database, stream_ids: list[int], owner: str) -> None:
    await _release(db, _EDU[0], stream_ids, owner)


async def delete_edus(db: Database, stream_ids: list[int], owner: str) -> None:
    await _delete(db, _EDU[0], stream_ids, owner)


# --- draining ----------------------------------------------------------------


async def destinations_with_pending(db: Database, now_ms: int) -> list[str]:
    """Destinations with at least one currently-claimable (unleased) row in *either*
    outbox, so the flusher retries EDU-only destinations too (not just PDU ones)."""
    rows = await db.fetchall(
        "SELECT DISTINCT destination FROM federation_outbox WHERE leased_until < ?"
        " UNION"
        " SELECT DISTINCT destination FROM federation_edu_outbox WHERE leased_until < ?",
        (now_ms, now_ms),
    )
    return [str(row[0]) for row in rows]
