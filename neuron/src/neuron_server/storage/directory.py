# SPDX-License-Identifier: Apache-2.0
"""Data access for the room directory: local aliases and the published flag.

Two small tables (migration 25):

- ``room_aliases`` maps a local alias (``#localpart:server_name``) to a room id,
  recording who created the mapping so only they (or a room admin) may delete it.
- ``room_directory`` holds each room's public-directory visibility; an absent row
  means ``private`` (the default — not listed in ``/publicRooms``).
"""

from __future__ import annotations

from neuron_server.storage.database import Database

# The spec caps a room alias at 255 bytes.
_MAX_ALIAS_BYTES = 255


def is_valid_alias(alias: str) -> bool:
    """Whether ``alias`` is a syntactically valid ``#localpart:server_name``."""
    if not alias.startswith("#") or ":" not in alias:
        return False
    if len(alias.encode("utf-8")) > _MAX_ALIAS_BYTES:
        return False
    localpart, server = alias[1:].split(":", 1)
    return bool(localpart) and bool(server)


def alias_server(alias: str) -> str:
    """The server-name part of an alias (everything after the first ``:``)."""
    return alias.split(":", 1)[1]


# --- aliases ---------------------------------------------------------------


async def resolve_alias(db: Database, alias: str) -> str | None:
    """The room id an alias maps to, or ``None`` if unknown."""
    value = await db.fetchval("SELECT room_id FROM room_aliases WHERE alias = ?", (alias,))
    return None if value is None else str(value)


async def get_alias_creator(db: Database, alias: str) -> str | None:
    """The user who created the alias mapping, or ``None`` if unknown."""
    value = await db.fetchval("SELECT creator FROM room_aliases WHERE alias = ?", (alias,))
    return None if value is None else str(value)


async def create_alias(
    db: Database, alias: str, room_id: str, creator: str, created_ts: int
) -> bool:
    """Create an alias mapping; return ``False`` if the alias already exists."""
    if await resolve_alias(db, alias) is not None:
        return False
    await db.execute(
        "INSERT INTO room_aliases (alias, room_id, creator, created_ts) VALUES (?, ?, ?, ?)",
        (alias, room_id, creator, created_ts),
    )
    return True


async def delete_alias(db: Database, alias: str) -> None:
    await db.execute("DELETE FROM room_aliases WHERE alias = ?", (alias,))


async def aliases_for_room(db: Database, room_id: str) -> list[str]:
    rows = await db.fetchall(
        "SELECT alias FROM room_aliases WHERE room_id = ? ORDER BY alias", (room_id,)
    )
    return [str(row[0]) for row in rows]


# --- published (public-directory) flag -------------------------------------


async def get_visibility(db: Database, room_id: str) -> str:
    """A room's public-directory visibility (``public``/``private``; default private)."""
    value = await db.fetchval(
        "SELECT visibility FROM room_directory WHERE room_id = ?", (room_id,)
    )
    return str(value) if value is not None else "private"


async def set_visibility(db: Database, room_id: str, visibility: str) -> None:
    await db.execute(
        "INSERT INTO room_directory (room_id, visibility) VALUES (?, ?)"
        " ON CONFLICT(room_id) DO UPDATE SET visibility = excluded.visibility",
        (room_id, visibility),
    )


async def published_room_ids(db: Database) -> list[str]:
    """Every room id currently published (visibility ``public``), ordered stably."""
    rows = await db.fetchall(
        "SELECT room_id FROM room_directory WHERE visibility = 'public' ORDER BY room_id"
    )
    return [str(row[0]) for row in rows]
