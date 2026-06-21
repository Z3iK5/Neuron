# SPDX-License-Identifier: Apache-2.0
"""Data access for media metadata (the ``media`` table)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from neuron_server.storage.database import Database


@dataclass
class MediaRow:
    media_id: str
    content_type: str
    upload_name: str | None
    size: int
    uploader: str
    created_ts: int


async def create_media(
    db: Database,
    media_id: str,
    content_type: str,
    upload_name: str | None,
    size: int,
    uploader: str,
    created_ts: int,
) -> None:
    await db.execute(
        "INSERT INTO media (media_id, content_type, upload_name, size, uploader, created_ts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (media_id, content_type, upload_name, size, uploader, created_ts),
    )


def _row_to_media(row: tuple[Any, ...]) -> MediaRow:
    return MediaRow(
        media_id=str(row[0]),
        content_type=str(row[1]),
        upload_name=None if row[2] is None else str(row[2]),
        size=int(row[3]),
        uploader=str(row[4]),
        created_ts=int(row[5]),
    )


async def get_media(db: Database, media_id: str) -> MediaRow | None:
    rows = await db.fetchall(
        "SELECT media_id, content_type, upload_name, size, uploader, created_ts"
        " FROM media WHERE media_id = ?",
        (media_id,),
    )
    return _row_to_media(rows[0]) if rows else None


def _uploader_filter(uploader: str | None) -> tuple[str, tuple[Any, ...]]:
    """A ``WHERE`` clause + params filtering by uploader substring (or no filter)."""
    if not uploader:
        return "", ()
    return " WHERE uploader LIKE ?", (f"%{uploader}%",)


async def count_media(db: Database, *, uploader: str | None = None) -> int:
    where, params = _uploader_filter(uploader)
    return int(await db.fetchval(f"SELECT COUNT(*) FROM media{where}", params) or 0)


async def total_media_bytes(db: Database, *, uploader: str | None = None) -> int:
    where, params = _uploader_filter(uploader)
    return int(await db.fetchval(f"SELECT COALESCE(SUM(size), 0) FROM media{where}", params) or 0)


async def list_media(
    db: Database, *, offset: int, limit: int, uploader: str | None = None
) -> list[MediaRow]:
    where, params = _uploader_filter(uploader)
    rows = await db.fetchall(
        "SELECT media_id, content_type, upload_name, size, uploader, created_ts"
        f" FROM media{where} ORDER BY created_ts DESC, media_id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return [_row_to_media(r) for r in rows]


async def delete_media(db: Database, media_id: str) -> None:
    await db.execute("DELETE FROM media WHERE media_id = ?", (media_id,))
