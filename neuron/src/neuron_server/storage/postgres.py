# SPDX-License-Identifier: Apache-2.0
"""PostgreSQL backend (via ``asyncpg``) — for production deployments.

SQL is written with ``?`` placeholders for portability; here we translate them to
PostgreSQL's positional ``$1``/``$2`` style. (Our queries never contain a literal
``?`` inside a string, so a straight positional substitution is safe.)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from neuron_server.storage.database import Database


def _to_pg(sql: str) -> str:
    """Translate ``?`` placeholders to PostgreSQL ``$1``, ``$2``, ... order."""
    out: list[str] = []
    n = 0
    for ch in sql:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


class PostgresDatabase(Database):
    """A single-connection async PostgreSQL database."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None

    async def connect(self) -> None:
        import asyncpg

        self._conn = await asyncpg.connect(self._dsn)

    async def disconnect(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self._conn.execute(_to_pg(sql), *tuple(params))

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        rows = await self._conn.fetch(_to_pg(sql), *tuple(params))
        return [tuple(row) for row in rows]

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return await self._conn.fetchval(_to_pg(sql), *tuple(params))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        async with self._conn.transaction():
            yield
