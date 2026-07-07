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


async def pending_backlog(db: Database) -> dict[str, tuple[int, int]]:
    """Per-destination pending backlog as ``{destination: (pdu_count, edu_count)}``."""
    combined: dict[str, tuple[int, int]] = {}
    for table, is_edu in (("federation_outbox", False), ("federation_edu_outbox", True)):
        rows = await db.fetchall(
            f"SELECT destination, COUNT(*) FROM {table} GROUP BY destination"
        )
        for dest, count in rows:
            pdu, edu = combined.get(str(dest), (0, 0))
            combined[str(dest)] = (pdu, int(count)) if is_edu else (int(count), edu)
    return combined
