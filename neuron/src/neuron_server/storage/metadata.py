# SPDX-License-Identifier: Apache-2.0
"""Server-level key/value metadata helpers (the ``server_metadata`` table).

A small store for facts about the server instance itself — for now the server
name (used to guard against pointing one database at a differently-named server);
later phases keep e.g. the server's signing key here.
"""

from __future__ import annotations

from neuron_server.storage.database import Database


async def get_metadata(db: Database, key: str) -> str | None:
    """Return the stored value for ``key``, or ``None`` if unset."""
    return await db.fetchval("SELECT value FROM server_metadata WHERE key = ?", (key,))


async def set_metadata(db: Database, key: str, value: str) -> None:
    """Insert or update ``key`` -> ``value`` (upsert)."""
    await db.execute(
        "INSERT INTO server_metadata (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
