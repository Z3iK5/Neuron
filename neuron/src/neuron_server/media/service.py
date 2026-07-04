# SPDX-License-Identifier: Apache-2.0
"""Media repository service: upload, download, thumbnail, config.

Stores blobs via a :class:`MediaStore` and metadata in the ``media`` table.
Media IDs are opaque, server-generated, and validated on the way back in to
prevent path traversal. Only **local** media is served — fetching remote media
over federation is part of the federation epic (HS-7).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from neuron_server.clock import now_ms
from neuron_server.errors import MatrixError
from neuron_server.media.store import MediaStore
from neuron_server.media.thumbnails import make_thumbnail
from neuron_server.storage import media as store
from neuron_server.storage.database import Database

_MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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
        self, store_backend: MediaStore, db: Database, server_name: str, max_upload_bytes: int
    ) -> None:
        self._store = store_backend
        self._db = db
        self._server_name = server_name
        self._max_upload_bytes = max_upload_bytes

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
        if server_name != self._server_name:
            raise MatrixError(404, "M_NOT_FOUND", "Remote media is not available")
        if not _MEDIA_ID_RE.match(media_id):
            raise MatrixError(404, "M_NOT_FOUND", "Invalid media ID")
        row = await store.get_media(self._db, media_id)
        if row is None:
            raise MatrixError(404, "M_NOT_FOUND", "Media not found")
        data = await self._store.get(media_id)
        if data is None:
            raise MatrixError(404, "M_NOT_FOUND", "Media content missing")
        return MediaContent(data=data, content_type=row.content_type, upload_name=row.upload_name)

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
