# SPDX-License-Identifier: Apache-2.0
"""Tests for the neuron_server storage layer (uses temp-file SQLite; no server)."""

from __future__ import annotations

from pathlib import Path

from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.metadata import get_metadata, set_metadata
from neuron_server.storage.migrations import MIGRATIONS, run_migrations


async def _connected(url: str) -> Database:
    db = connect_database(url)
    await db.connect()
    return db


async def test_migrations_run_and_are_idempotent(tmp_path: Path) -> None:
    db = await _connected(f"sqlite:///{tmp_path / 'hs.db'}")
    try:
        first = await run_migrations(db)
        assert first == [m.version for m in MIGRATIONS]

        # Every applied migration is recorded in schema_migrations.
        rows = await db.fetchall("SELECT version FROM schema_migrations")
        assert {row[0] for row in rows} == set(first)

        # Running again applies nothing (idempotent).
        assert await run_migrations(db) == []
    finally:
        await db.disconnect()


async def test_metadata_get_set_and_upsert(tmp_path: Path) -> None:
    db = await _connected(f"sqlite:///{tmp_path / 'hs.db'}")
    try:
        await run_migrations(db)

        assert await get_metadata(db, "server_name") is None
        await set_metadata(db, "server_name", "neuron.local")
        assert await get_metadata(db, "server_name") == "neuron.local"

        # A second set for the same key overwrites (upsert).
        await set_metadata(db, "server_name", "other.example")
        assert await get_metadata(db, "server_name") == "other.example"
    finally:
        await db.disconnect()


async def test_in_memory_sqlite_url(tmp_path: Path) -> None:
    # The :memory: form should work for the single-connection database.
    db = await _connected("sqlite:///:memory:")
    try:
        await run_migrations(db)
        await set_metadata(db, "k", "v")
        assert await get_metadata(db, "k") == "v"
    finally:
        await db.disconnect()
