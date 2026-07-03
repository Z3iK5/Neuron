# SPDX-License-Identifier: Apache-2.0
"""Data access for E2EE relay: device keys, one-time keys, cross-signing,
to-device messages, and device-list change tracking.

The server only **stores and relays** this material — it never decrypts anything.
Stream IDs (to-device, device-list) are assigned as ``MAX+1`` within a serialized
transaction, like the event stream, so we stay portable across SQLite/Postgres.
"""

from __future__ import annotations

import json
from typing import Any

from neuron_server.storage.database import Database

# --- device identity keys --------------------------------------------------


async def upsert_device_keys(db: Database, user_id: str, device_id: str, key_json: str) -> None:
    await db.execute(
        "INSERT INTO device_keys (user_id, device_id, key_json) VALUES (?, ?, ?)"
        " ON CONFLICT(user_id, device_id) DO UPDATE SET key_json = excluded.key_json",
        (user_id, device_id, key_json),
    )


async def get_device_keys(db: Database, user_id: str, device_id: str) -> dict[str, Any] | None:
    value = await db.fetchval(
        "SELECT key_json FROM device_keys WHERE user_id = ? AND device_id = ?",
        (user_id, device_id),
    )
    return None if value is None else json.loads(str(value))


async def get_device_keys_for_user(db: Database, user_id: str) -> dict[str, dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT device_id, key_json FROM device_keys WHERE user_id = ?",
        (user_id,),
    )
    return {str(r[0]): json.loads(str(r[1])) for r in rows}


# --- one-time keys ---------------------------------------------------------


async def store_one_time_keys(
    db: Database, user_id: str, device_id: str, keys: dict[str, Any]
) -> None:
    for key_alg_id, key_obj in keys.items():
        await db.execute(
            "INSERT INTO one_time_keys (user_id, device_id, key_alg_id, key_json)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(user_id, device_id, key_alg_id)"
            " DO UPDATE SET key_json = excluded.key_json",
            (user_id, device_id, key_alg_id, json.dumps(key_obj)),
        )


async def count_one_time_keys(db: Database, user_id: str, device_id: str) -> dict[str, int]:
    rows = await db.fetchall(
        "SELECT key_alg_id FROM one_time_keys WHERE user_id = ? AND device_id = ?",
        (user_id, device_id),
    )
    counts: dict[str, int] = {}
    for row in rows:
        algorithm = str(row[0]).split(":", 1)[0]
        counts[algorithm] = counts.get(algorithm, 0) + 1
    return counts


async def claim_one_time_key(
    db: Database, user_id: str, device_id: str, algorithm: str
) -> dict[str, Any] | None:
    """Atomically remove and return one OTK ``{key_alg_id: key_obj}`` for the device.

    The pick and the delete are one ``DELETE ... RETURNING`` statement, so a key is
    only returned if *this* claimer removed it. A SELECT-then-DELETE would let two
    concurrent Postgres transactions (READ COMMITTED, pool > 1) select the same row
    and both hand it out, breaking Olm's single-use guarantee; here the loser's
    DELETE matches no row and it falls through to the fallback key. (RETURNING
    needs SQLite >= 3.35, 2021 — universal on supported platforms.)
    """
    rows = await db.fetchall(
        "DELETE FROM one_time_keys WHERE user_id = ? AND device_id = ? AND key_alg_id = ("
        " SELECT key_alg_id FROM one_time_keys"
        " WHERE user_id = ? AND device_id = ? AND key_alg_id LIKE ?"
        " ORDER BY key_alg_id LIMIT 1"
        ") RETURNING key_alg_id, key_json",
        (user_id, device_id, user_id, device_id, f"{algorithm}:%"),
    )
    if rows:
        return {str(rows[0][0]): json.loads(str(rows[0][1]))}

    # Fall back to the device's unused fallback key (not consumed).
    fallback = await db.fetchall(
        "SELECT key_alg_id, key_json FROM fallback_keys"
        " WHERE user_id = ? AND device_id = ? AND algorithm = ?",
        (user_id, device_id, algorithm),
    )
    if fallback:
        return {str(fallback[0][0]): json.loads(str(fallback[0][1]))}
    return None


async def store_fallback_keys(
    db: Database, user_id: str, device_id: str, keys: dict[str, Any]
) -> None:
    for key_alg_id, key_obj in keys.items():
        algorithm = key_alg_id.split(":", 1)[0]
        await db.execute(
            "INSERT INTO fallback_keys (user_id, device_id, algorithm, key_alg_id, key_json, used)"
            " VALUES (?, ?, ?, ?, ?, 0)"
            " ON CONFLICT(user_id, device_id, algorithm) DO UPDATE SET"
            " key_alg_id = excluded.key_alg_id, key_json = excluded.key_json, used = 0",
            (user_id, device_id, algorithm, key_alg_id, json.dumps(key_obj)),
        )


# --- cross-signing keys ----------------------------------------------------


async def upsert_cross_signing_key(
    db: Database, user_id: str, key_type: str, key_json: str
) -> None:
    await db.execute(
        "INSERT INTO cross_signing_keys (user_id, key_type, key_json) VALUES (?, ?, ?)"
        " ON CONFLICT(user_id, key_type) DO UPDATE SET key_json = excluded.key_json",
        (user_id, key_type, key_json),
    )


async def get_cross_signing_key(
    db: Database, user_id: str, key_type: str
) -> dict[str, Any] | None:
    value = await db.fetchval(
        "SELECT key_json FROM cross_signing_keys WHERE user_id = ? AND key_type = ?",
        (user_id, key_type),
    )
    return None if value is None else json.loads(str(value))


# --- to-device messages ----------------------------------------------------


async def add_to_device_message(
    db: Database,
    target_user: str,
    target_device: str,
    sender: str,
    event_type: str,
    content: dict[str, Any],
) -> int:
    stream_id = await db.next_stream_id("to_device")
    await db.execute(
        "INSERT INTO to_device_messages"
        " (stream_id, target_user, target_device, sender, type, content_json)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (stream_id, target_user, target_device, sender, event_type, json.dumps(content)),
    )
    return stream_id


async def get_to_device(
    db: Database, user_id: str, device_id: str, after_stream: int, limit: int
) -> list[tuple[int, dict[str, Any]]]:
    rows = await db.fetchall(
        "SELECT stream_id, sender, type, content_json FROM to_device_messages"
        " WHERE target_user = ? AND target_device = ? AND stream_id > ?"
        " ORDER BY stream_id ASC LIMIT ?",
        (user_id, device_id, after_stream, limit),
    )
    out: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        out.append(
            (
                int(row[0]),
                {"sender": str(row[1]), "type": str(row[2]), "content": json.loads(str(row[3]))},
            )
        )
    return out


async def delete_to_device_up_to(
    db: Database, user_id: str, device_id: str, up_to_stream: int
) -> None:
    await db.execute(
        "DELETE FROM to_device_messages"
        " WHERE target_user = ? AND target_device = ? AND stream_id <= ?",
        (user_id, device_id, up_to_stream),
    )


# --- device-list change tracking -------------------------------------------


async def bump_device_list(db: Database, user_id: str) -> int:
    stream_id = await db.next_stream_id("device_lists")
    await db.execute(
        "INSERT INTO device_list_changes (stream_id, user_id) VALUES (?, ?)",
        (stream_id, user_id),
    )
    return stream_id


async def get_device_list_changes_after(db: Database, after_stream: int) -> list[str]:
    rows = await db.fetchall(
        "SELECT DISTINCT user_id FROM device_list_changes WHERE stream_id > ?",
        (after_stream,),
    )
    return [str(row[0]) for row in rows]
