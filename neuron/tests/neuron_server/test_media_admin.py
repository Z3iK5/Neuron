# SPDX-License-Identifier: Apache-2.0
"""Admin media listing + purge: storage queries, MediaService.delete, AdminService."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from neuron_server.admin.service import AdminService
from neuron_server.errors import MatrixError
from neuron_server.media.service import MediaService
from neuron_server.media.store import FilesystemMediaStore
from neuron_server.storage import media as media_store
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.migrations import run_migrations


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = connect_database("sqlite:///:memory:")
    await database.connect()
    await run_migrations(database)
    try:
        yield database
    finally:
        await database.disconnect()


def _service(db: Database, tmp_path: Path) -> MediaService:
    return MediaService(FilesystemMediaStore(str(tmp_path)), db, "neuron.local", 50 * 1024 * 1024)


async def test_count_list_and_bytes_with_uploader_filter(db: Database, tmp_path: Path) -> None:
    media = _service(db, tmp_path)
    await media.upload("@alice:neuron.local", b"x" * 10, "text/plain", "a.txt")
    await media.upload("@alice:neuron.local", b"y" * 20, "text/plain", "b.txt")
    await media.upload("@bob:neuron.local", b"z" * 30, "image/png", None)

    assert await media_store.count_media(db) == 3
    assert await media_store.total_media_bytes(db) == 60
    # Uploader filter is a substring match.
    assert await media_store.count_media(db, uploader="alice") == 2
    assert await media_store.total_media_bytes(db, uploader="alice") == 30
    bob = await media_store.list_media(db, offset=0, limit=10, uploader="bob")
    assert [m.uploader for m in bob] == ["@bob:neuron.local"]


async def test_list_media_paginates(db: Database, tmp_path: Path) -> None:
    media = _service(db, tmp_path)
    ids = set()
    for i in range(3):
        uri = await media.upload("@a:neuron.local", bytes([i]) * (i + 1), "text/plain", None)
        ids.add(uri.rsplit("/", 1)[1])
    first2 = await media_store.list_media(db, offset=0, limit=2)
    rest = await media_store.list_media(db, offset=2, limit=2)
    assert len(first2) == 2 and len(rest) == 1
    assert {m.media_id for m in first2 + rest} == ids


async def test_delete_removes_metadata_and_blob(db: Database, tmp_path: Path) -> None:
    media = _service(db, tmp_path)
    uri = await media.upload("@a:neuron.local", b"hello", "text/plain", "h.txt")
    mid = uri.rsplit("/", 1)[1]
    assert (await media.download("neuron.local", mid)).data == b"hello"

    assert await media.delete(mid) is True
    assert await media_store.get_media(db, mid) is None
    with pytest.raises(MatrixError):  # download now 404s (metadata gone)
        await media.download("neuron.local", mid)
    # Deleting again is a no-op (already gone).
    assert await media.delete(mid) is False


async def test_delete_rejects_bad_media_id(db: Database, tmp_path: Path) -> None:
    media = _service(db, tmp_path)
    assert await media.delete("../etc/passwd") is False


async def test_admin_service_lists_and_deletes(db: Database, tmp_path: Path) -> None:
    media = _service(db, tmp_path)
    uri = await media.upload("@a:neuron.local", b"data", "text/plain", None)
    mid = uri.rsplit("/", 1)[1]
    admin = AdminService(db, "neuron.local", media=media)

    body = await admin.list_media(offset=0, limit=10)
    assert body["total"] == 1 and body["total_bytes"] == 4

    assert await admin.delete_media(mid) is True
    assert (await admin.list_media(offset=0, limit=10))["total"] == 0


async def test_admin_service_delete_requires_media(db: Database) -> None:
    admin = AdminService(db, "neuron.local")  # no MediaService wired
    with pytest.raises(MatrixError):
        await admin.delete_media("abc")
