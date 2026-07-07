# SPDX-License-Identifier: Apache-2.0
"""Data access for the thumbnail cache (the ``media_thumbnails`` table).

Maps a ``(origin_server, media_id, width, height, method)`` variant to the local
blob ``cache_key`` under which we stored the generated thumbnail bytes, so a later
request for the same variant is served from our store instead of re-decoding and
re-encoding the original. Mirrors ``remote_media.py``: a cache table plus a
namespaced blob key (see ``media/service.py`` for the key derivation).
"""

from __future__ import annotations

from dataclasses import dataclass

from neuron_server.storage.database import Database


@dataclass
class ThumbnailRow:
    origin_server: str
    media_id: str
    width: int
    height: int
    method: str
    cache_key: str
    content_type: str
    size: int
    created_ts: int


async def get_thumbnail(
    db: Database, origin_server: str, media_id: str, width: int, height: int, method: str
) -> ThumbnailRow | None:
    rows = await db.fetchall(
        "SELECT origin_server, media_id, width, height, method, cache_key,"
        " content_type, size, created_ts FROM media_thumbnails"
        " WHERE origin_server = ? AND media_id = ? AND width = ? AND height = ?"
        " AND method = ?",
        (origin_server, media_id, width, height, method),
    )
    if not rows:
        return None
    row = rows[0]
    return ThumbnailRow(
        origin_server=str(row[0]),
        media_id=str(row[1]),
        width=int(row[2]),
        height=int(row[3]),
        method=str(row[4]),
        cache_key=str(row[5]),
        content_type=str(row[6]),
        size=int(row[7]),
        created_ts=int(row[8]),
    )


async def create_thumbnail(
    db: Database,
    origin_server: str,
    media_id: str,
    width: int,
    height: int,
    method: str,
    cache_key: str,
    content_type: str,
    size: int,
    created_ts: int,
) -> None:
    """Record a cached thumbnail entry, ignoring a concurrent duplicate.

    ``ON CONFLICT DO NOTHING`` makes two simultaneous generations of the same
    variant idempotent: whichever inserts first wins and the other is a no-op (the
    cache_key is derived deterministically, so both wrote the same key).
    """
    await db.execute(
        "INSERT INTO media_thumbnails"
        " (origin_server, media_id, width, height, method, cache_key,"
        " content_type, size, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (origin_server, media_id, width, height, method) DO NOTHING",
        (
            origin_server,
            media_id,
            width,
            height,
            method,
            cache_key,
            content_type,
            size,
            created_ts,
        ),
    )


async def list_thumbnail_keys(db: Database, origin_server: str, media_id: str) -> list[str]:
    """The blob cache_keys for a media's cached thumbnails, so a delete can drop them."""
    rows = await db.fetchall(
        "SELECT cache_key FROM media_thumbnails WHERE origin_server = ? AND media_id = ?",
        (origin_server, media_id),
    )
    return [str(r[0]) for r in rows]


async def delete_thumbnails(db: Database, origin_server: str, media_id: str) -> None:
    await db.execute(
        "DELETE FROM media_thumbnails WHERE origin_server = ? AND media_id = ?",
        (origin_server, media_id),
    )
