# SPDX-License-Identifier: Apache-2.0
"""Media repository service: upload, download, thumbnail, config.

Stores blobs via a :class:`MediaStore` and metadata in the ``media`` table.
Media IDs are opaque, server-generated, and validated on the way back in to
prevent path traversal. Media from other servers is fetched over federation and
cached locally (the ``remote_media_cache`` table + a namespaced blob key).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

from neuron_server.clock import now_ms
from neuron_server.errors import MatrixError
from neuron_server.federation.client import FederationClient, RemoteMediaTooLarge
from neuron_server.media.multipart import parse_multipart
from neuron_server.media.store import MediaStore
from neuron_server.media.thumbnails import make_thumbnail
from neuron_server.storage import media as store
from neuron_server.storage import remote_media as remote_store
from neuron_server.storage.database import Database

_MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Blob-store key for cached remote media. The "remote_" prefix + a hash of
# (server_name, media_id) means (a) it can never collide with a LOCAL media id
# (those are ``secrets.token_hex(16)`` — 32 lowercase hex chars, no prefix), so a
# remote server can't overwrite or read a local blob, and (b) two servers using the
# same media id map to different keys.
def _remote_cache_key(server_name: str, media_id: str) -> str:
    digest = hashlib.sha256(f"{server_name}\x00{media_id}".encode()).hexdigest()
    return f"remote_{digest}"


def _media_part(parts: list[tuple[dict[str, str], bytes]]) -> tuple[str, bytes] | None:
    """Pick the media (non-JSON) part of a parsed federation multipart response.

    The spec puts JSON metadata first and the media second; we take the first part
    that isn't ``application/json`` (falling back to the last part) so we tolerate a
    peer that reorders or omits the metadata part.
    """
    if not parts:
        return None
    for headers, data in parts:
        ctype = headers.get("content-type", "")
        if ctype.split(";", 1)[0].strip().lower() != "application/json":
            return ctype or "application/octet-stream", data
    headers, data = parts[-1]
    return headers.get("content-type", "application/octet-stream"), data

# Content types we are willing to serve inline; everything else is sent as an
# attachment (Content-Disposition) to avoid the browser rendering it (XSS safety).
_INLINE_PREFIXES = ("image/", "video/", "audio/")
_INLINE_EXACT = frozenset({"text/plain"})


@dataclass
class MediaContent:
    data: bytes
    content_type: str
    upload_name: str | None



class MediaService:
    """Handles media uploads and retrieval for one server."""

    def __init__(
        self,
        store_backend: MediaStore,
        db: Database,
        server_name: str,
        max_upload_bytes: int,
        *,
        federation_client: FederationClient | None = None,
        max_remote_media_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self._store = store_backend
        self._db = db
        self._server_name = server_name
        self._max_upload_bytes = max_upload_bytes
        self._federation_client = federation_client
        self._max_remote_media_bytes = max_remote_media_bytes

    def config(self) -> dict[str, int]:
        return {"m.upload.size": self._max_upload_bytes}

    async def upload(
        self, uploader: str, data: bytes, content_type: str, upload_name: str | None
    ) -> str:
        if len(data) > self._max_upload_bytes:
            raise MatrixError(413, "M_TOO_LARGE", "Upload is too large")
        media_id = secrets.token_hex(16)
        await self._store.put(media_id, data)
        await store.create_media(
            self._db, media_id, content_type, upload_name, len(data), uploader, now_ms()
        )
        return f"mxc://{self._server_name}/{media_id}"

    async def download(self, server_name: str, media_id: str) -> MediaContent:
        # Validate first (both paths), so a traversal-shaped id never reaches a store
        # key or a federation URL.
        if not _MEDIA_ID_RE.match(media_id):
            raise MatrixError(404, "M_NOT_FOUND", "Invalid media ID")
        if server_name != self._server_name:
            return await self._download_remote(server_name, media_id)
        row = await store.get_media(self._db, media_id)
        if row is None:
            raise MatrixError(404, "M_NOT_FOUND", "Media not found")
        data = await self._store.get(media_id)
        if data is None:
            raise MatrixError(404, "M_NOT_FOUND", "Media content missing")
        return MediaContent(data=data, content_type=row.content_type, upload_name=row.upload_name)

    async def _download_remote(self, server_name: str, media_id: str) -> MediaContent:
        """Serve remote media from the local cache, fetching over federation on a miss."""
        cached = await remote_store.get_remote_media(self._db, server_name, media_id)
        if cached is not None:
            data = await self._store.get(cached.cache_key)
            if data is not None:
                return MediaContent(
                    data=data,
                    content_type=cached.content_type,
                    upload_name=cached.upload_name,
                )
            # Row present but blob gone (e.g. store wiped): fall through and re-fetch.
        if self._federation_client is None:
            raise MatrixError(404, "M_NOT_FOUND", "Remote media is not available")

        try:
            content_type_header, body = await self._federation_client.get_media(
                server_name,
                f"/_matrix/federation/v1/media/download/{media_id}",
                max_bytes=self._max_remote_media_bytes,
            )
        except RemoteMediaTooLarge as exc:
            raise MatrixError(502, "M_TOO_LARGE", "Remote media exceeds the size limit") from exc
        except Exception as exc:
            # Unreachable origin, upstream 404, transport error: to the client this is
            # simply "media unavailable".
            raise MatrixError(404, "M_NOT_FOUND", "Remote media is not available") from exc

        picked = _media_part(parse_multipart(content_type_header, body))
        if picked is None:
            raise MatrixError(404, "M_NOT_FOUND", "Remote media is not available")
        content_type, data = picked
        if len(data) > self._max_remote_media_bytes:
            raise MatrixError(502, "M_TOO_LARGE", "Remote media exceeds the size limit")

        cache_key = _remote_cache_key(server_name, media_id)
        await self._store.put(cache_key, data)
        await remote_store.create_remote_media(
            self._db, server_name, media_id, cache_key, content_type, None, len(data), now_ms()
        )
        return MediaContent(data=data, content_type=content_type, upload_name=None)

    async def delete(self, media_id: str) -> bool:
        """Delete local media (metadata + blob). Returns False if it didn't exist.

        Metadata is removed first so the item immediately stops being listed and
        served; the blob is then removed (idempotently). A stray blob left by a
        failed object-store delete only wastes disk — it can never be downloaded
        once its metadata is gone.
        """
        if not _MEDIA_ID_RE.match(media_id):
            return False
        if await store.get_media(self._db, media_id) is None:
            return False
        await store.delete_media(self._db, media_id)
        await self._store.delete(media_id)
        return True

    async def thumbnail(
        self, server_name: str, media_id: str, width: int, height: int, method: str
    ) -> MediaContent:
        original = await self.download(server_name, media_id)
        thumb = make_thumbnail(original.data, width, height, method)
        if thumb is None:
            # Not an image we can resize — fall back to the original content.
            return original
        data, content_type = thumb
        return MediaContent(data=data, content_type=content_type, upload_name=None)

    @staticmethod
    def disposition_type(content_type: str) -> str:
        """Whether content of this type may be served inline or must be an attachment."""
        base = content_type.split(";", 1)[0].strip().lower()
        if base in _INLINE_EXACT or base.startswith(_INLINE_PREFIXES):
            return "inline"
        return "attachment"
