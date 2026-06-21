# SPDX-License-Identifier: Apache-2.0
"""Data access backing the Synapse-compatible Admin API: user listing/admin
flags and registration tokens."""

from __future__ import annotations

import json
import secrets
from typing import Any

from neuron_server.storage.accounts import UserRow
from neuron_server.storage.database import Database, escape_like


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
        where.append("u.name LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(name)}%")
    if deactivated is not None:
        where.append("u.deactivated = ?")
        params.append(1 if deactivated else 0)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.extend([limit, offset])
    rows = await db.fetchall(
        "SELECT u.name, u.admin, u.deactivated, u.created_ts, p.displayname, u.shadow_banned"
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
            "shadow_banned": bool(r[5]),
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


async def set_user_shadow_banned(db: Database, name: str, shadow_banned: bool) -> None:
    await db.execute(
        "UPDATE users SET shadow_banned = ? WHERE name = ?",
        (1 if shadow_banned else 0, name),
    )


def user_to_admin_dict(row: UserRow, displayname: str | None) -> dict[str, Any]:
    return {
        "name": row.name,
        "admin": row.admin,
        "deactivated": row.deactivated,
        "creation_ts": row.created_ts,
        "displayname": displayname,
        "shadow_banned": row.shadow_banned,
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


# --- moderation: deletion/redaction status, reports, server-notices rooms ---


async def record_room_deletion(
    db: Database, room_id: str, kicked_users: list[str], *, ts: int
) -> str:
    """Store a completed room deletion and return its delete_id."""
    delete_id = secrets.token_urlsafe(8)
    await db.execute(
        "INSERT INTO room_deletions (delete_id, room_id, status, kicked_users, created_ts)"
        " VALUES (?, ?, 'complete', ?, ?)",
        (delete_id, room_id, json.dumps(kicked_users), ts),
    )
    return delete_id


async def get_room_deletion(db: Database, delete_id: str) -> dict[str, Any] | None:
    rows = await db.fetchall(
        "SELECT status, kicked_users FROM room_deletions WHERE delete_id = ?", (delete_id,)
    )
    if not rows:
        return None
    return {
        "status": str(rows[0][0]),
        "shutdown_room": {
            "kicked_users": json.loads(rows[0][1]),
            "failed_to_kick_users": [],
        },
    }


async def record_redaction(
    db: Database, user_id: str, total: int, failed: list[str], *, ts: int
) -> str:
    """Store a completed bulk redaction and return its redact_id."""
    redact_id = secrets.token_urlsafe(8)
    await db.execute(
        "INSERT INTO room_redactions (redact_id, user_id, status, total, failed, created_ts)"
        " VALUES (?, ?, 'complete', ?, ?, ?)",
        (redact_id, user_id, total, json.dumps(failed), ts),
    )
    return redact_id


async def get_redaction(db: Database, redact_id: str) -> dict[str, Any] | None:
    rows = await db.fetchall(
        "SELECT status, failed FROM room_redactions WHERE redact_id = ?", (redact_id,)
    )
    if not rows:
        return None
    failed = json.loads(rows[0][1])
    return {
        "status": str(rows[0][0]),
        "failed_redactions": {event_id: "failed" for event_id in failed},
    }


async def add_event_report(
    db: Database,
    *,
    room_id: str,
    event_id: str,
    reporter: str,
    reason: str | None,
    score: int | None,
    ts: int,
) -> None:
    await db.execute(
        "INSERT INTO event_reports (id, room_id, event_id, reporter, reason, score, received_ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (secrets.token_urlsafe(8), room_id, event_id, reporter, reason, score, ts),
    )


_REPORT_COLS = "id, room_id, event_id, reporter, reason, score, received_ts"


def _event_report_dict(r: Any) -> dict[str, Any]:
    return {
        "id": str(r[0]),
        "room_id": str(r[1]),
        "event_id": str(r[2]),
        "user_id": str(r[3]),  # the reporter (Synapse Admin API naming)
        "reason": None if r[4] is None else str(r[4]),
        "score": None if r[5] is None else int(r[5]),
        "received_ts": int(r[6]),
    }


async def list_event_reports(
    db: Database, *, offset: int = 0, limit: int = 100
) -> tuple[list[dict[str, Any]], int]:
    total = int(await db.fetchval("SELECT COUNT(*) FROM event_reports"))
    rows = await db.fetchall(
        f"SELECT {_REPORT_COLS} FROM event_reports"
        " ORDER BY received_ts DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [_event_report_dict(r) for r in rows], total


async def get_event_report(db: Database, report_id: str) -> dict[str, Any] | None:
    rows = await db.fetchall(
        f"SELECT {_REPORT_COLS} FROM event_reports WHERE id = ?", (report_id,)
    )
    return _event_report_dict(rows[0]) if rows else None


async def delete_event_report(db: Database, report_id: str) -> None:
    await db.execute("DELETE FROM event_reports WHERE id = ?", (report_id,))


async def get_server_notices_room(db: Database, user_id: str) -> str | None:
    value = await db.fetchval(
        "SELECT room_id FROM server_notices_rooms WHERE user_id = ?", (user_id,)
    )
    return None if value is None else str(value)


async def set_server_notices_room(db: Database, user_id: str, room_id: str) -> None:
    await db.execute(
        "INSERT INTO server_notices_rooms (user_id, room_id) VALUES (?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET room_id = excluded.room_id",
        (user_id, room_id),
    )


# --- passkeys (WebAuthn) ---------------------------------------------------


def _passkey_row(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "credential_id": str(r[0]),
        "owner": str(r[1]),
        "public_key": str(r[2]),
        "sign_count": int(r[3]),
        "label": str(r[4]),
        "created_ts": int(r[5]),
    }


_PASSKEY_COLS = "credential_id, owner, public_key, sign_count, label, created_ts"


async def list_passkeys(db: Database, owner: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        f"SELECT {_PASSKEY_COLS} FROM passkeys WHERE owner = ? ORDER BY created_ts", (owner,)
    )
    return [_passkey_row(r) for r in rows]


async def all_passkey_ids(db: Database) -> list[str]:
    rows = await db.fetchall("SELECT credential_id FROM passkeys")
    return [str(r[0]) for r in rows]


async def get_passkey(db: Database, credential_id: str) -> dict[str, Any] | None:
    rows = await db.fetchall(
        f"SELECT {_PASSKEY_COLS} FROM passkeys WHERE credential_id = ?", (credential_id,)
    )
    return _passkey_row(rows[0]) if rows else None


async def add_passkey(
    db: Database,
    *,
    credential_id: str,
    owner: str,
    public_key: str,
    sign_count: int,
    label: str,
    ts: int,
) -> None:
    await db.execute(
        f"INSERT INTO passkeys ({_PASSKEY_COLS}) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(credential_id) DO UPDATE SET public_key = excluded.public_key,"
        " sign_count = excluded.sign_count, label = excluded.label",
        (credential_id, owner, public_key, sign_count, label, ts),
    )


async def remove_passkey(db: Database, owner: str, credential_id: str) -> None:
    await db.execute(
        "DELETE FROM passkeys WHERE owner = ? AND credential_id = ?", (owner, credential_id)
    )


async def set_passkey_sign_count(db: Database, credential_id: str, sign_count: int) -> None:
    await db.execute(
        "UPDATE passkeys SET sign_count = ? WHERE credential_id = ?", (sign_count, credential_id)
    )
