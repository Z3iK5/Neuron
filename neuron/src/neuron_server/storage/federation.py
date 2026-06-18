# SPDX-License-Identifier: Apache-2.0
"""Storage for federation state: the cache of remote servers' verify keys."""

from __future__ import annotations

from neuron_server.storage.database import Database


async def get_cached_server_keys(
    db: Database, server_name: str, now_ms: int
) -> dict[str, str]:
    """Return ``{key_id: verify_key}`` for ``server_name`` that are still valid."""
    rows = await db.fetchall(
        "SELECT key_id, verify_key FROM remote_server_keys"
        " WHERE server_name = ? AND valid_until_ts > ?",
        (server_name, now_ms),
    )
    return {str(key_id): str(verify_key) for key_id, verify_key in rows}


async def cache_server_keys(
    db: Database, server_name: str, keys: dict[str, str], valid_until_ts: int
) -> None:
    """Upsert ``server_name``'s verify keys with their validity horizon."""
    async with db.transaction():
        for key_id, verify_key in keys.items():
            await db.execute(
                "INSERT INTO remote_server_keys (server_name, key_id, verify_key, valid_until_ts)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(server_name, key_id) DO UPDATE SET"
                " verify_key = excluded.verify_key, valid_until_ts = excluded.valid_until_ts",
                (server_name, key_id, verify_key, valid_until_ts),
            )
