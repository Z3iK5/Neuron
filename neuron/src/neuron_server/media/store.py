# SPDX-License-Identifier: Apache-2.0
"""Blob storage for media content.

A minimal interface so the bytes can live on a local filesystem (development) or,
later, an S3-compatible bucket (production). HS-4 ships the filesystem backend.
Files are sharded by the first two characters of the media ID to avoid enormous
directories. Disk I/O runs in a worker thread so it never blocks the event loop.
"""

from __future__ import annotations

import abc
import asyncio
from pathlib import Path


class MediaStore(abc.ABC):
    """Stores and retrieves media blobs by media ID."""

    @abc.abstractmethod
    async def put(self, media_id: str, data: bytes) -> None: ...

    @abc.abstractmethod
    async def get(self, media_id: str) -> bytes | None: ...


class FilesystemMediaStore(MediaStore):
    """Stores blobs as files under a base directory."""

    def __init__(self, base_path: str) -> None:
        self._base = Path(base_path)

    def _path(self, media_id: str) -> Path:
        return self._base / media_id[:2] / media_id

    async def put(self, media_id: str, data: bytes) -> None:
        path = self._path(media_id)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)

    async def get(self, media_id: str) -> bytes | None:
        path = self._path(media_id)

        def _read() -> bytes | None:
            return path.read_bytes() if path.is_file() else None

        return await asyncio.to_thread(_read)
