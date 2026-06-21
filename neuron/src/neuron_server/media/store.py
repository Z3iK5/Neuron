# SPDX-License-Identifier: Apache-2.0
"""Blob storage for media content.

A minimal interface so the bytes can live on a local filesystem (the desktop /
single-host default) or an S3-compatible bucket (for multi-host / multi-worker
deployments, where a shared filesystem isn't available). Filesystem blobs are
sharded by the first two characters of the media ID to avoid enormous directories.
All I/O runs in a worker thread so it never blocks the event loop.
"""

from __future__ import annotations

import abc
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neuron_server.config import NeuronServerSettings


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


class S3MediaStore(MediaStore):
    """Stores blobs in an S3 (or S3-compatible, e.g. MinIO) bucket.

    Required for multi-host deployments: every worker reads/writes the same bucket
    instead of a local disk. Credentials come from the standard AWS chain
    (``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` env, instance role, etc.), so
    they never live in Neuron config. ``boto3`` is imported lazily, so the
    filesystem/desktop path needs neither the dependency nor any S3 setup.

    The synchronous ``boto3`` client runs in a worker thread (like the filesystem
    backend); the bytes are already bounded by the upload size limit, so whole-blob
    get/put is fine.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        client: Any = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._endpoint_url = endpoint_url
        self._region = region
        self._client = client  # injectable for tests; otherwise created on first use

    def _key(self, media_id: str) -> str:
        return f"{self._prefix}{media_id}"

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url or None,
                region_name=self._region or None,
            )
        return self._client

    async def put(self, media_id: str, data: bytes) -> None:
        client = self._get_client()
        await asyncio.to_thread(
            client.put_object, Bucket=self._bucket, Key=self._key(media_id), Body=data
        )

    async def get(self, media_id: str) -> bytes | None:
        client = self._get_client()

        def _read() -> bytes | None:
            try:
                response = client.get_object(Bucket=self._bucket, Key=self._key(media_id))
            except client.exceptions.NoSuchKey:
                return None
            body: bytes = response["Body"].read()
            return body

        return await asyncio.to_thread(_read)


def build_media_store(settings: NeuronServerSettings) -> MediaStore:
    """Select the media backend for this deployment.

    ``filesystem`` (the default) uses a local directory — right for the desktop and
    single-host servers. ``s3`` uses an S3-compatible bucket so multiple workers /
    hosts share media. ``boto3`` is imported lazily by :class:`S3MediaStore`, so the
    default path stays dependency-free.
    """
    backend = settings.media_backend
    if backend == "filesystem":
        return FilesystemMediaStore(settings.media_store_path)
    if backend == "s3":
        if not settings.s3_media_bucket:
            raise ValueError("media_backend='s3' requires NEURON_SERVER_S3_MEDIA_BUCKET")
        return S3MediaStore(
            settings.s3_media_bucket,
            prefix=settings.s3_media_prefix,
            endpoint_url=settings.s3_media_endpoint_url or None,
            region=settings.s3_media_region or None,
        )
    raise ValueError(f"unknown media_backend: {backend!r}")
