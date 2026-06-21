# SPDX-License-Identifier: Apache-2.0
"""Tests for the media blob backends and the backend factory.

The S3 store is exercised with a fake client injected in, so these run without
``boto3`` installed (it is lazily imported only when a real S3 client is created).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from neuron_server.config import NeuronServerSettings
from neuron_server.media.store import (
    FilesystemMediaStore,
    S3MediaStore,
    build_media_store,
)


def _settings(**kw: Any) -> NeuronServerSettings:
    return NeuronServerSettings(name="neuron.local", **kw)


def test_build_media_store_filesystem_is_default(tmp_path: Path) -> None:
    store = build_media_store(_settings(media_store_path=str(tmp_path)))
    assert isinstance(store, FilesystemMediaStore)


def test_build_media_store_s3_needs_no_boto3_to_construct() -> None:
    store = build_media_store(
        _settings(media_backend="s3", s3_media_bucket="b", s3_media_prefix="m/")
    )
    assert isinstance(store, S3MediaStore)


def test_build_media_store_s3_requires_bucket() -> None:
    with pytest.raises(ValueError, match="S3_MEDIA_BUCKET"):
        build_media_store(_settings(media_backend="s3"))


def test_build_media_store_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown media_backend"):
        build_media_store(_settings(media_backend="bogus"))


class _FakeS3Client:
    """Mimics the slice of the boto3 S3 client S3MediaStore uses."""

    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions namespace
        class NoSuchKey(Exception):
            pass

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        try:
            data = self.objects[(Bucket, Key)]
        except KeyError:
            raise self.exceptions.NoSuchKey from None
        return {"Body": io.BytesIO(data)}


async def test_s3_store_put_get_roundtrip() -> None:
    fake = _FakeS3Client()
    store = S3MediaStore("bucket", prefix="media/", client=fake)
    await store.put("abc123", b"hello world")
    # The configured prefix is applied to the key.
    assert ("bucket", "media/abc123") in fake.objects
    assert await store.get("abc123") == b"hello world"


async def test_s3_store_get_missing_returns_none() -> None:
    store = S3MediaStore("bucket", client=_FakeS3Client())
    assert await store.get("does-not-exist") is None
