# SPDX-License-Identifier: Apache-2.0
"""Per-destination federation delivery health (the ``federation_destinations``
table, migration 29).

One row per remote server we've sent a transaction to, recording the last
success/failure timestamps, a run of ``consecutive_failures`` (reset to 0 on any
success) and a SHORT ``last_error`` string. The sender writes these best-effort
(see :mod:`neuron_server.federation.sender`); the console's Federation page reads
them, joined with the outbox backlog, to show whether delivery is healthy.

``last_error`` is only ever an exception class plus a truncated message — never
key material or event content. SQL is portable ``?``-placeholder / ``ON CONFLICT``
so it works on both SQLite and PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass

from neuron_server.storage.database import Database


@dataclass(frozen=True)
class DestinationHealth:
    """One row of ``federation_destinations``."""

    destination: str
    last_success_ts: int | None
    last_failure_ts: int | None
    consecutive_failures: int
    last_error: str | None


@dataclass(frozen=True)
class DestinationBacklog:
    """Pending outbox rows for a destination (PDUs + EDUs)."""

    pdu_pending: int
    edu_pending: int
    oldest_stream_id: int | None


async def record_success(db: Database, destination: str, now_ms: int) -> None:
    """Mark a successful transaction: set ``last_success_ts``, reset the failure run
    to 0 and clear ``last_error`` (upsert)."""
    await db.execute(
        "INSERT INTO federation_destinations"
        " (destination, last_success_ts, consecutive_failures, last_error)"
        " VALUES (?, ?, 0, NULL)"
        " ON CONFLICT(destination) DO UPDATE SET"
        " last_success_ts = excluded.last_success_ts,"
        " consecutive_failures = 0,"
        " last_error = NULL",
        (destination, now_ms),
    )


async def record_failure(db: Database, destination: str, now_ms: int, error: str) -> None:
    """Mark a failed transaction: set ``last_failure_ts``, increment the failure run
    and store a SHORT ``error`` string (upsert)."""
    await db.execute(
        "INSERT INTO federation_destinations"
        " (destination, last_failure_ts, consecutive_failures, last_error)"
        " VALUES (?, ?, 1, ?)"
        " ON CONFLICT(destination) DO UPDATE SET"
        " last_failure_ts = excluded.last_failure_ts,"
        " consecutive_failures = federation_destinations.consecutive_failures + 1,"
        " last_error = excluded.last_error",
        (destination, now_ms, error),
    )


async def list_destinations(db: Database) -> list[DestinationHealth]:
    """All recorded destination-health rows."""
    rows = await db.fetchall(
        "SELECT destination, last_success_ts, last_failure_ts, consecutive_failures, last_error"
        " FROM federation_destinations"
    )
    return [
        DestinationHealth(
            destination=str(r[0]),
            last_success_ts=None if r[1] is None else int(r[1]),
            last_failure_ts=None if r[2] is None else int(r[2]),
            consecutive_failures=int(r[3]),
            last_error=None if r[4] is None else str(r[4]),
        )
        for r in rows
    ]


async def pending_backlog(db: Database) -> dict[str, DestinationBacklog]:
    """Per-destination pending backlog across both outboxes: PDU count, EDU count
    and the oldest (lowest) pending ``stream_id``."""
    combined: dict[str, tuple[int, int, int | None]] = {}
    for table, is_edu in (("federation_outbox", False), ("federation_edu_outbox", True)):
        rows = await db.fetchall(
            f"SELECT destination, COUNT(*), MIN(stream_id) FROM {table} GROUP BY destination"
        )
        for dest, count, oldest in rows:
            dest = str(dest)
            pdu, edu, cur_oldest = combined.get(dest, (0, 0, None))
            oldest_int = None if oldest is None else int(oldest)
            merged_oldest = min(
                x for x in (cur_oldest, oldest_int) if x is not None
            ) if (cur_oldest is not None or oldest_int is not None) else None
            if is_edu:
                combined[dest] = (pdu, int(count), merged_oldest)
            else:
                combined[dest] = (int(count), edu, merged_oldest)
    return {
        dest: DestinationBacklog(pdu_pending=pdu, edu_pending=edu, oldest_stream_id=oldest)
        for dest, (pdu, edu, oldest) in combined.items()
    }
