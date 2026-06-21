# SPDX-License-Identifier: Apache-2.0
"""Data access for User-Interactive-Auth sessions (the ``uia_sessions`` table)."""

from __future__ import annotations

from neuron_server.storage.database import Database


async def create_session(db: Database, session_id: str, created_ts: int) -> None:
    await db.execute(
        "INSERT INTO uia_sessions (session_id, created_ts) VALUES (?, ?)",
        (session_id, created_ts),
    )


async def session_exists(db: Database, session_id: str, min_created_ts: int = 0) -> bool:
    """True if the session exists and was created at/after ``min_created_ts``.

    The TTL is enforced here (not only by the background sweep) so an expired but
    not-yet-swept session is rejected on the read path.
    """
    row = await db.fetchval(
        "SELECT 1 FROM uia_sessions WHERE session_id = ? AND created_ts >= ?",
        (session_id, min_created_ts),
    )
    return row is not None


async def delete_session(db: Database, session_id: str) -> None:
    await db.execute("DELETE FROM uia_sessions WHERE session_id = ?", (session_id,))


async def delete_expired(db: Database, cutoff_ts: int) -> None:
    """Remove sessions created before ``cutoff_ts`` (bounds table growth)."""
    await db.execute("DELETE FROM uia_sessions WHERE created_ts < ?", (cutoff_ts,))
