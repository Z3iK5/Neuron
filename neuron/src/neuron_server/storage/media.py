# SPDX-License-Identifier: Apache-2.0
"""Data access for media metadata (the ``media`` table)."""

from __future__ import annotations

from dataclasses import dataclass

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


async def get_media(db: Database, media_id: str) -> MediaRow | None:
    rows = await db.fetchall(
        "SELECT media_id, content_type, upload_name, size, uploader, created_ts"
        " FROM media WHERE media_id = ?",
        (media_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return MediaRow(
        media_id=str(row[0]),
        content_type=str(row[1]),
        upload_name=None if row[2] is None else str(row[2]),
        size=int(row[3]),
        uploader=str(row[4]),
        created_ts=int(row[5]),
    )
