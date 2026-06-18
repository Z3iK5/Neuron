# SPDX-License-Identifier: Apache-2.0
"""Data access backing the Synapse-compatible Admin API: user listing/admin
flags and registration tokens."""

from __future__ import annotations

from typing import Any

from neuron_server.storage.accounts import UserRow
from neuron_server.storage.database import Database


async def count_users(db: Database, *, deactivated: bool | None) -> int:
    if deactivated is None:
        value = await db.fetchval("SELECT COUNT(*) FROM users")
    else:
        value = await db.fetchval(
            "SELECT COUNT(*) FROM users WHERE deactivated = ?", (1 if deactivated else 0,)
        )
    return int(value)


async def list_users(
    db: Database, *, offset: int, limit: int, name: str | None, deactivated: bool | None
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if name:
        where.append("u.name LIKE ?")
        params.append(f"%{name}%")
    if deactivated is not None:
        where.append("u.deactivated = ?")
        params.append(1 if deactivated else 0)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.extend([limit, offset])
    rows = await db.fetchall(
        "SELECT u.name, u.admin, u.deactivated, u.created_ts, p.displayname"
        " FROM users u LEFT JOIN profiles p ON p.user_id = u.name"
        f"{clause} ORDER BY u.name LIMIT ? OFFSET ?",
        params,
    )
    return [
        {
            "name": str(r[0]),
            "admin": bool(r[1]),
            "deactivated": bool(r[2]),
            "creation_ts": int(r[3]),
            "displayname": None if r[4] is None else str(r[4]),
            "shadow_banned": False,
            "is_guest": False,
            "user_type": None,
        }
        for r in rows
    ]


async def set_user_admin(db: Database, name: str, admin: bool) -> None:
    await db.execute("UPDATE users SET admin = ? WHERE name = ?", (1 if admin else 0, name))


async def set_user_password(db: Database, name: str, password_hash: str) -> None:
    await db.execute("UPDATE users SET password_hash = ? WHERE name = ?", (password_hash, name))


async def set_user_deactivated(db: Database, name: str, deactivated: bool) -> None:
    await db.execute(
        "UPDATE users SET deactivated = ? WHERE name = ?", (1 if deactivated else 0, name)
    )


def user_to_admin_dict(row: UserRow, displayname: str | None) -> dict[str, Any]:
    return {
        "name": row.name,
        "admin": row.admin,
        "deactivated": row.deactivated,
        "creation_ts": row.created_ts,
        "displayname": displayname,
        "shadow_banned": False,
        "is_guest": False,
        "user_type": None,
        "threepids": [],
        "external_ids": [],
        "erased": False,
    }


# --- registration tokens ---------------------------------------------------


async def list_registration_tokens(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT token, uses_allowed, pending, completed, expiry_time FROM registration_tokens"
    )
    return [
        {
            "token": str(r[0]),
            "uses_allowed": None if r[1] is None else int(r[1]),
            "pending": int(r[2]),
            "completed": int(r[3]),
            "expiry_time": None if r[4] is None else int(r[4]),
        }
        for r in rows
    ]


async def get_registration_token(db: Database, token: str) -> dict[str, Any] | None:
    rows = await db.fetchall(
        "SELECT token, uses_allowed, pending, completed, expiry_time"
        " FROM registration_tokens WHERE token = ?",
        (token,),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "token": str(r[0]),
        "uses_allowed": None if r[1] is None else int(r[1]),
        "pending": int(r[2]),
        "completed": int(r[3]),
        "expiry_time": None if r[4] is None else int(r[4]),
    }


async def create_registration_token(
    db: Database, token: str, uses_allowed: int | None, expiry_time: int | None
) -> None:
    await db.execute(
        "INSERT INTO registration_tokens (token, uses_allowed, pending, completed, expiry_time)"
        " VALUES (?, ?, 0, 0, ?)"
        " ON CONFLICT(token) DO UPDATE SET uses_allowed = excluded.uses_allowed,"
        " expiry_time = excluded.expiry_time",
        (token, uses_allowed, expiry_time),
    )


async def delete_registration_token(db: Database, token: str) -> None:
    await db.execute("DELETE FROM registration_tokens WHERE token = ?", (token,))


def _token_usable(row: dict[str, Any], now_ms: int) -> bool:
    """True if ``row`` is unexpired and still has at least one use remaining."""
    expiry = row["expiry_time"]
    if expiry is not None and now_ms > expiry:
        return False
    allowed = row["uses_allowed"]
    return allowed is None or int(row["completed"]) < allowed


async def registration_token_valid(db: Database, token: str, now_ms: int) -> bool:
    """True if ``token`` exists, is unexpired and has a use left (no consumption)."""
    row = await get_registration_token(db, token)
    return row is not None and _token_usable(row, now_ms)


async def consume_registration_token(db: Database, token: str, now_ms: int) -> bool:
    """Claim one use of ``token`` atomically. Returns False if invalid/expired/spent."""
    async with db.transaction():
        row = await get_registration_token(db, token)
        if row is None or not _token_usable(row, now_ms):
            return False
        await db.execute(
            "UPDATE registration_tokens SET completed = completed + 1 WHERE token = ?",
            (token,),
        )
        return True
