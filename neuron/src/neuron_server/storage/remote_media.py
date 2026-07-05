# SPDX-License-Identifier: Apache-2.0
"""Data access for the remote-media cache (the ``remote_media_cache`` table).

Maps a remote server's ``(origin_server, origin_media_id)`` to the local blob
``cache_key`` under which we stored the fetched bytes, so a later download of the
same remote media is served from our store instead of re-fetching over federation.
"""

from __future__ import annotations

from dataclasses import dataclass

from neuron_server.storage.database import Database


@dataclass
class RemoteMediaRow:
    origin_server: str
    origin_media_id: str
    cache_key: str
    content_type: str
    upload_name: str | None
    size: int
    fetched_ts: int


async def get_remote_media(
    db: Database, origin_server: str, origin_media_id: str
) -> RemoteMediaRow | None:
    rows = await db.fetchall(
        "SELECT origin_server, origin_media_id, cache_key, content_type, upload_name,"
        " size, fetched_ts FROM remote_media_cache"
        " WHERE origin_server = ? AND origin_media_id = ?",
        (origin_server, origin_media_id),
    )
    if not rows:
        return None
    row = rows[0]
    return RemoteMediaRow(
        origin_server=str(row[0]),
        origin_media_id=str(row[1]),
        cache_key=str(row[2]),
        content_type=str(row[3]),
        upload_name=None if row[4] is None else str(row[4]),
        size=int(row[5]),
        fetched_ts=int(row[6]),
    )


async def create_remote_media(
    db: Database,
    origin_server: str,
    origin_media_id: str,
    cache_key: str,
    content_type: str,
    upload_name: str | None,
    size: int,
    fetched_ts: int,
) -> None:
    """Record a cached remote-media entry, ignoring a concurrent duplicate.

    ``ON CONFLICT DO NOTHING`` makes two simultaneous fetches of the same remote
    media idempotent: whichever inserts first wins and the other is a no-op (both
    wrote the same bytes to the same cache key, so the row is equivalent).
    """
    await db.execute(
        "INSERT INTO remote_media_cache"
        " (origin_server, origin_media_id, cache_key, content_type, upload_name,"
        " size, fetched_ts) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (origin_server, origin_media_id) DO NOTHING",
        (origin_server, origin_media_id, cache_key, content_type, upload_name, size, fetched_ts),
    )
