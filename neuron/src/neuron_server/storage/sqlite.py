# SPDX-License-Identifier: Apache-2.0
"""SQLite backend (via ``aiosqlite``) — the default for development and tests.

The connection runs in autocommit mode (``isolation_level=None``) so we control
transactions explicitly with ``BEGIN``/``COMMIT``/``ROLLBACK``; this keeps DDL
(used by migrations) behaving predictably inside :meth:`transaction`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from neuron_server.storage.database import STREAMS, Database


class SQLiteDatabase(Database):
    """A single-connection async SQLite database."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Any = None
        # Serializes multi-statement transactions: with a single connection,
        # concurrent BEGIN/COMMIT from different tasks must not interleave.
        self._tx_lock = asyncio.Lock()

    async def connect(self) -> None:
        import aiosqlite

        # Autocommit mode: no implicit transactions; we manage them ourselves.
        self._conn = await aiosqlite.connect(self._path, isolation_level=None)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets readers run concurrently with the single writer (a no-op for
        # :memory: databases, which ignore it and stay in "memory" journal mode).
        await self._conn.execute("PRAGMA journal_mode = WAL")

    async def disconnect(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self._conn.execute(sql, tuple(params))

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        cursor = await self._conn.execute(sql, tuple(params))
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        return [tuple(row) for row in rows]

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cursor = await self._conn.execute(sql, tuple(params))
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        return row[0] if row else None

    async def next_stream_id(self, name: str) -> int:
        # MAX(col)+1 is race-free here: the single connection serializes all writes.
        table, col = STREAMS[name]
        return int(
            await self.fetchval(f"SELECT COALESCE(MAX({col}), 0) + 1 FROM {table}")
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        async with self._tx_lock:
            await self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
            else:
                await self._conn.execute("COMMIT")
