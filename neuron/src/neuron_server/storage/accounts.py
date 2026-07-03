# SPDX-License-Identifier: Apache-2.0
"""Data access for accounts: users, devices, and access tokens.

Thin async functions over the :class:`Database` interface — one concern per
function, portable SQL. Higher-level orchestration (registration, login, token
issuance) lives in :mod:`neuron_server.auth.service`.
"""

from __future__ import annotations

from dataclasses import dataclass

from neuron_server.storage.database import Database, escape_like


@dataclass
class UserRow:
    """A row from the ``users`` table."""

    name: str
    password_hash: str | None
    admin: bool
    deactivated: bool
    created_ts: int
    shadow_banned: bool = False


@dataclass
class DeviceRow:
    """A row from the ``devices`` table."""

    device_id: str
    display_name: str | None
    created_ts: int


# --- users -----------------------------------------------------------------


async def create_user(
    db: Database, name: str, password_hash: str | None, admin: bool, created_ts: int
) -> None:
    await db.execute(
        "INSERT INTO users (name, password_hash, admin, deactivated, created_ts)"
        " VALUES (?, ?, ?, 0, ?)",
        (name, password_hash, 1 if admin else 0, created_ts),
    )


async def get_user(db: Database, name: str) -> UserRow | None:
    rows = await db.fetchall(
        "SELECT name, password_hash, admin, deactivated, created_ts, shadow_banned"
        " FROM users WHERE name = ?",
        (name,),
    )
    if not rows:
        return None
    row = rows[0]
    return UserRow(
        name=row[0],
        password_hash=row[1],
        admin=bool(row[2]),
        deactivated=bool(row[3]),
        created_ts=int(row[4]),
        shadow_banned=bool(row[5]),
    )


async def user_exists(db: Database, name: str) -> bool:
    return (await db.fetchval("SELECT 1 FROM users WHERE name = ?", (name,))) is not None


async def any_users(db: Database) -> bool:
    """True if at least one account exists (used to grant the first user admin)."""
    return (await db.fetchval("SELECT 1 FROM users LIMIT 1")) is not None


async def search_users(
    db: Database, search_term: str, server_name: str, limit: int
) -> list[tuple[str, str | None, str | None]]:
    """Case-insensitively match local accounts for the user directory.

    Matches ``search_term`` against the localpart (the part of ``users.name``
    before ``:server_name``) and the profile displayname. Deactivated accounts
    are excluded. Returns up to ``limit`` ``(user_id, displayname, avatar_url)``
    rows, ordered by user id.
    """
    escaped = escape_like(search_term.lower())
    # Localparts cannot contain ':', so anything matched before ":server" is
    # within the localpart.
    localpart_pattern = f"@%{escaped}%:{server_name.lower()}"
    displayname_pattern = f"%{escaped}%"
    rows = await db.fetchall(
        "SELECT u.name, p.displayname, p.avatar_url"
        " FROM users u LEFT JOIN profiles p ON p.user_id = u.name"
        " WHERE u.deactivated = 0"
        " AND (LOWER(u.name) LIKE ? ESCAPE '\\'"
        " OR LOWER(COALESCE(p.displayname, '')) LIKE ? ESCAPE '\\')"
        " ORDER BY u.name LIMIT ?",
        (localpart_pattern, displayname_pattern, limit),
    )
    return [
        (
            str(row[0]),
            None if row[1] is None else str(row[1]),
            None if row[2] is None else str(row[2]),
        )
        for row in rows
    ]


# --- devices ---------------------------------------------------------------


async def create_device(
    db: Database, user_id: str, device_id: str, display_name: str | None, created_ts: int
) -> None:
    await db.execute(
        "INSERT INTO devices (user_id, device_id, display_name, created_ts)"
        " VALUES (?, ?, ?, ?)",
        (user_id, device_id, display_name, created_ts),
    )


async def device_exists(db: Database, user_id: str, device_id: str) -> bool:
    value = await db.fetchval(
        "SELECT 1 FROM devices WHERE user_id = ? AND device_id = ?",
        (user_id, device_id),
    )
    return value is not None


async def get_device(db: Database, user_id: str, device_id: str) -> DeviceRow | None:
    rows = await db.fetchall(
        "SELECT device_id, display_name, created_ts FROM devices"
        " WHERE user_id = ? AND device_id = ?",
        (user_id, device_id),
    )
    if not rows:
        return None
    row = rows[0]
    return DeviceRow(device_id=row[0], display_name=row[1], created_ts=int(row[2]))


async def list_devices(db: Database, user_id: str) -> list[DeviceRow]:
    rows = await db.fetchall(
        "SELECT device_id, display_name, created_ts FROM devices WHERE user_id = ?"
        " ORDER BY created_ts",
        (user_id,),
    )
    return [DeviceRow(device_id=r[0], display_name=r[1], created_ts=int(r[2])) for r in rows]


async def set_device_display_name(
    db: Database, user_id: str, device_id: str, display_name: str | None
) -> None:
    await db.execute(
        "UPDATE devices SET display_name = ? WHERE user_id = ? AND device_id = ?",
        (display_name, user_id, device_id),
    )


async def delete_device(db: Database, user_id: str, device_id: str) -> None:
    await db.execute(
        "DELETE FROM devices WHERE user_id = ? AND device_id = ?",
        (user_id, device_id),
    )


async def delete_all_devices(db: Database, user_id: str) -> None:
    await db.execute("DELETE FROM devices WHERE user_id = ?", (user_id,))


# --- access tokens ---------------------------------------------------------


async def create_access_token(
    db: Database, token: str, user_id: str, device_id: str, created_ts: int
) -> None:
    await db.execute(
        "INSERT INTO access_tokens (token, user_id, device_id, created_ts)"
        " VALUES (?, ?, ?, ?)",
        (token, user_id, device_id, created_ts),
    )


async def get_token(db: Database, token: str) -> tuple[str, str] | None:
    rows = await db.fetchall(
        "SELECT user_id, device_id FROM access_tokens WHERE token = ?",
        (token,),
    )
    if not rows:
        return None
    return (rows[0][0], rows[0][1])


async def delete_token(db: Database, token: str) -> None:
    await db.execute("DELETE FROM access_tokens WHERE token = ?", (token,))


async def delete_tokens_for_device(db: Database, user_id: str, device_id: str) -> None:
    await db.execute(
        "DELETE FROM access_tokens WHERE user_id = ? AND device_id = ?",
        (user_id, device_id),
    )


async def delete_tokens_for_user(db: Database, user_id: str) -> None:
    await db.execute("DELETE FROM access_tokens WHERE user_id = ?", (user_id,))
