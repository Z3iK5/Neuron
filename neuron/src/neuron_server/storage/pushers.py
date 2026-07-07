# SPDX-License-Identifier: Apache-2.0
"""Data access for pushers — a user's registered push targets.

A pusher is where a user's notifications are delivered: for a phone it is a
device token (``pushkey``) plus the push gateway URL (``data.url``). Uniqueness
is ``(user_id, app_id, pushkey)`` per the Client-Server spec.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from neuron_server.storage.database import Database


@dataclass(frozen=True)
class Pusher:
    app_id: str
    pushkey: str
    kind: str
    app_display_name: str | None
    device_display_name: str | None
    profile_tag: str | None
    lang: str | None
    data: dict[str, Any]
    ts: int

    def to_client(self) -> dict[str, Any]:
        return {
            "pushkey": self.pushkey,
            "kind": self.kind,
            "app_id": self.app_id,
            "app_display_name": self.app_display_name,
            "device_display_name": self.device_display_name,
            "profile_tag": self.profile_tag,
            "lang": self.lang,
            "data": self.data,
        }


_COLUMNS = (
    "app_id, pushkey, kind, app_display_name, device_display_name,"
    " profile_tag, lang, data_json, ts"
)


def _row(row: tuple[Any, ...]) -> Pusher:
    (app_id, pushkey, kind, app_dn, device_dn, profile_tag, lang, data_json, ts) = row
    return Pusher(
        app_id=str(app_id),
        pushkey=str(pushkey),
        kind=str(kind),
        app_display_name=None if app_dn is None else str(app_dn),
        device_display_name=None if device_dn is None else str(device_dn),
        profile_tag=None if profile_tag is None else str(profile_tag),
        lang=None if lang is None else str(lang),
        data=json.loads(str(data_json)) if data_json else {},
        ts=int(ts),
    )


async def get_pushers(db: Database, user_id: str) -> list[Pusher]:
    rows = await db.fetchall(
        f"SELECT {_COLUMNS} FROM pushers WHERE user_id = ? ORDER BY app_id, pushkey",
        (user_id,),
    )
    return [_row(row) for row in rows]


async def upsert_pusher(
    db: Database,
    user_id: str,
    *,
    app_id: str,
    pushkey: str,
    kind: str,
    app_display_name: str | None,
    device_display_name: str | None,
    profile_tag: str | None,
    lang: str | None,
    data: dict[str, Any],
    ts: int,
) -> None:
    await db.execute(
        "INSERT INTO pushers"
        " (user_id, app_id, pushkey, kind, app_display_name, device_display_name,"
        "  profile_tag, lang, data_json, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, app_id, pushkey) DO UPDATE SET"
        " kind = excluded.kind, app_display_name = excluded.app_display_name,"
        " device_display_name = excluded.device_display_name,"
        " profile_tag = excluded.profile_tag, lang = excluded.lang,"
        " data_json = excluded.data_json, ts = excluded.ts",
        (
            user_id,
            app_id,
            pushkey,
            kind,
            app_display_name,
            device_display_name,
            profile_tag,
            lang,
            json.dumps(data),
            ts,
        ),
    )


async def delete_pusher(db: Database, user_id: str, app_id: str, pushkey: str) -> None:
    await db.execute(
        "DELETE FROM pushers WHERE user_id = ? AND app_id = ? AND pushkey = ?",
        (user_id, app_id, pushkey),
    )


async def delete_pushkey_elsewhere(
    db: Database, user_id: str, app_id: str, pushkey: str
) -> None:
    """Remove this ``(app_id, pushkey)`` from every OTHER user (the ``append=false``
    semantics: a device token registered afresh must belong to one user only)."""
    await db.execute(
        "DELETE FROM pushers WHERE app_id = ? AND pushkey = ? AND user_id != ?",
        (app_id, pushkey, user_id),
    )
