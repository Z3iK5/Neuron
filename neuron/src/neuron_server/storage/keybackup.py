# SPDX-License-Identifier: Apache-2.0
"""Data access for server-side key backup (``/room_keys``).

The server stores encrypted megolm session keys per (user, backup version) but
never decrypts them. Versions are soft-deleted (their key rows are dropped) so
version numbers stay monotonically increasing per user. Each version carries an
integer ``etag`` counter bumped whenever its stored keys actually change.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from neuron_server.storage.database import Database


@dataclass(frozen=True)
class BackupVersion:
    """One (non-deleted) backup version row."""

    version: int
    algorithm: str
    auth_data: dict[str, Any]
    etag: int


# --- versions ----------------------------------------------------------------


async def create_version(
    db: Database, user_id: str, algorithm: str, auth_data: dict[str, Any]
) -> int:
    """Create a new backup version and return its (per-user monotonic) number.

    ``MAX+1`` includes soft-deleted rows, so a deleted version's number is never
    reused; the transaction serializes concurrent creates for the same user.
    """
    async with db.transaction():
        current = await db.fetchval(
            "SELECT MAX(version) FROM room_key_versions WHERE user_id = ?", (user_id,)
        )
        version = int(current or 0) + 1
        await db.execute(
            "INSERT INTO room_key_versions"
            " (user_id, version, algorithm, auth_data_json, etag, deleted, created_ts)"
            " VALUES (?, ?, ?, ?, 0, 0, ?)",
            (user_id, version, algorithm, json.dumps(auth_data), int(time.time() * 1000)),
        )
    return version


async def get_version(
    db: Database, user_id: str, version: int | None = None
) -> BackupVersion | None:
    """Return a specific non-deleted version, or the current (latest) one if None."""
    sql = (
        "SELECT version, algorithm, auth_data_json, etag FROM room_key_versions"
        " WHERE user_id = ? AND deleted = 0"
    )
    params: tuple[Any, ...] = (user_id,)
    if version is not None:
        sql += " AND version = ?"
        params = (user_id, version)
    rows = await db.fetchall(sql + " ORDER BY version DESC LIMIT 1", params)
    if not rows:
        return None
    row = rows[0]
    return BackupVersion(
        version=int(row[0]),
        algorithm=str(row[1]),
        auth_data=json.loads(str(row[2])),
        etag=int(row[3]),
    )


async def update_version_auth_data(
    db: Database, user_id: str, version: int, auth_data: dict[str, Any]
) -> None:
    await db.execute(
        "UPDATE room_key_versions SET auth_data_json = ?"
        " WHERE user_id = ? AND version = ? AND deleted = 0",
        (json.dumps(auth_data), user_id, version),
    )


async def delete_version(db: Database, user_id: str, version: int) -> None:
    """Soft-delete a version and drop its stored keys."""
    async with db.transaction():
        await db.execute(
            "UPDATE room_key_versions SET deleted = 1 WHERE user_id = ? AND version = ?",
            (user_id, version),
        )
        await db.execute(
            "DELETE FROM room_key_backups WHERE user_id = ? AND version = ?",
            (user_id, version),
        )


async def count_sessions(db: Database, user_id: str, version: int) -> int:
    value = await db.fetchval(
        "SELECT COUNT(*) FROM room_key_backups WHERE user_id = ? AND version = ?",
        (user_id, version),
    )
    return int(value or 0)


# --- keys --------------------------------------------------------------------


def _replaces(new: dict[str, Any], old: tuple[Any, ...]) -> bool:
    """The spec's replacement algorithm: higher ``is_verified`` wins, then lower
    ``first_message_index``, then higher ``forwarded_count``; otherwise keep."""
    old_fmi, old_fc, old_verified = int(old[0]), int(old[1]), bool(old[2])
    new_verified = bool(new["is_verified"])
    if new_verified != old_verified:
        return new_verified
    new_fmi = int(new["first_message_index"])
    if new_fmi != old_fmi:
        return new_fmi < old_fmi
    return int(new["forwarded_count"]) > old_fc


async def put_keys(
    db: Database,
    user_id: str,
    version: int,
    rooms: dict[str, dict[str, Any]],
) -> bool:
    """Store sessions (``{room_id: {"sessions": {session_id: KeyBackupData}}}``),
    replacing existing ones only when the new data is better per the spec's
    algorithm. Bumps the version's etag iff anything changed; returns whether it did.
    """
    changed = False
    async with db.transaction():
        for room_id, room in rooms.items():
            sessions = room.get("sessions", {})
            for session_id, data in sessions.items():
                existing = await db.fetchall(
                    "SELECT first_message_index, forwarded_count, is_verified"
                    " FROM room_key_backups"
                    " WHERE user_id = ? AND version = ? AND room_id = ? AND session_id = ?",
                    (user_id, version, room_id, session_id),
                )
                if existing and not _replaces(data, existing[0]):
                    continue
                await db.execute(
                    "INSERT INTO room_key_backups (user_id, version, room_id, session_id,"
                    " first_message_index, forwarded_count, is_verified, session_data_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(user_id, version, room_id, session_id) DO UPDATE SET"
                    " first_message_index = excluded.first_message_index,"
                    " forwarded_count = excluded.forwarded_count,"
                    " is_verified = excluded.is_verified,"
                    " session_data_json = excluded.session_data_json",
                    (
                        user_id,
                        version,
                        room_id,
                        session_id,
                        int(data["first_message_index"]),
                        int(data["forwarded_count"]),
                        1 if data["is_verified"] else 0,
                        json.dumps(data["session_data"]),
                    ),
                )
                changed = True
        if changed:
            await _bump_etag(db, user_id, version)
    return changed


async def get_keys(
    db: Database,
    user_id: str,
    version: int,
    room_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return ``{room_id: {"sessions": {session_id: KeyBackupData}}}`` for the
    version, optionally narrowed to one room or one session."""
    sql = (
        "SELECT room_id, session_id, first_message_index, forwarded_count,"
        " is_verified, session_data_json FROM room_key_backups"
        " WHERE user_id = ? AND version = ?"
    )
    params: list[Any] = [user_id, version]
    if room_id is not None:
        sql += " AND room_id = ?"
        params.append(room_id)
    if session_id is not None:
        sql += " AND session_id = ?"
        params.append(session_id)
    out: dict[str, dict[str, Any]] = {}
    for row in await db.fetchall(sql, tuple(params)):
        sessions = out.setdefault(str(row[0]), {"sessions": {}})["sessions"]
        sessions[str(row[1])] = {
            "first_message_index": int(row[2]),
            "forwarded_count": int(row[3]),
            "is_verified": bool(row[4]),
            "session_data": json.loads(str(row[5])),
        }
    return out


async def delete_keys(
    db: Database,
    user_id: str,
    version: int,
    room_id: str | None = None,
    session_id: str | None = None,
) -> bool:
    """Delete stored sessions (all / one room / one session); bump the etag iff
    any row was actually removed; return whether any was."""
    sql = "DELETE FROM room_key_backups WHERE user_id = ? AND version = ?"
    params: list[Any] = [user_id, version]
    if room_id is not None:
        sql += " AND room_id = ?"
        params.append(room_id)
    if session_id is not None:
        sql += " AND session_id = ?"
        params.append(session_id)
    async with db.transaction():
        rows = await db.fetchall(sql + " RETURNING session_id", tuple(params))
        if rows:
            await _bump_etag(db, user_id, version)
    return bool(rows)


async def _bump_etag(db: Database, user_id: str, version: int) -> None:
    await db.execute(
        "UPDATE room_key_versions SET etag = etag + 1 WHERE user_id = ? AND version = ?",
        (user_id, version),
    )
