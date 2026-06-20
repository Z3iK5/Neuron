# SPDX-License-Identifier: Apache-2.0
"""PostgreSQL backend (via ``asyncpg``) — for production deployments.

SQL is written with ``?`` placeholders for portability; here we translate them to
PostgreSQL's positional ``$1``/``$2`` style. (Our queries never contain a literal
``?`` inside a string, so a straight positional substitution is safe.)

Connections come from an ``asyncpg`` **pool**. A statement run outside a
transaction borrows a connection for that one call; inside :meth:`transaction`
the acquired connection is *pinned* (via a context variable) so every statement
in the block runs on the same connection — the connection-affinity the storage
layer needs, kept entirely internal so call sites pass ``db`` around unchanged.

The pool size defaults to **1**, which keeps writes serialized exactly like the
original single-connection backend. That default is deliberate: stream-id
allocation still uses ``MAX(col)+1``, which only stays race-free while a single
connection is in flight. Raise the pool size for real concurrency *after* IDs
come from database sequences.
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from neuron_server.storage.database import STREAMS, Database

# The connection pinned to the current transaction (None when not in one). A
# context variable rather than instance state, so concurrent tasks each see only
# their own transaction's connection.
_tx_conn: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "neuron_pg_tx_conn", default=None
)


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
    """An async PostgreSQL database backed by an ``asyncpg`` connection pool."""

    def __init__(self, dsn: str, *, pool_size: int = 1) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._pool_size = max(1, pool_size)

    async def connect(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=self._pool_size
        )

    async def disconnect(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[Any]:
        """Yield the current transaction's pinned connection, or borrow one."""
        pinned = _tx_conn.get()
        if pinned is not None:
            yield pinned
        else:
            async with self._pool.acquire() as conn:
                yield conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._conn() as conn:
            await conn.execute(_to_pg(sql), *tuple(params))

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        async with self._conn() as conn:
            rows = await conn.fetch(_to_pg(sql), *tuple(params))
        return [tuple(row) for row in rows]

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        async with self._conn() as conn:
            return await conn.fetchval(_to_pg(sql), *tuple(params))

    @staticmethod
    def _seq(name: str) -> str:
        return f"neuron_{name}_seq"

    async def ensure_stream_sequences(self) -> None:
        """Create each stream's SEQUENCE once, seeded above any existing rows.

        Seeded only when first created, never re-seeded — so a restart (or a
        second worker) never moves a live sequence backwards into ids another
        connection may have already handed out.
        """
        async with self._pool.acquire() as conn:
            for name, (table, col) in STREAMS.items():
                seq = self._seq(name)
                existed = await conn.fetchval(
                    "SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = $1", seq
                )
                await conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq}")
                if not existed:
                    start = int(
                        await conn.fetchval(f"SELECT COALESCE(MAX({col}), 0) + 1 FROM {table}")
                    )
                    # is_called=false -> the first nextval returns exactly ``start``.
                    await conn.execute(f"SELECT setval('{seq}', {start}, false)")

    async def next_stream_id(self, name: str) -> int:
        # nextval is non-transactional: concurrent connections get distinct ids
        # with no lock held for the duration of the caller's transaction.
        return int(await self.fetchval(f"SELECT nextval('{self._seq(name)}')"))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        pinned = _tx_conn.get()
        if pinned is not None:
            # Already in a transaction on this task — nest via a savepoint on the
            # same connection rather than acquiring (and deadlocking on) a second.
            async with pinned.transaction():
                yield
            return
        async with self._pool.acquire() as conn:
            token = _tx_conn.set(conn)
            try:
                async with conn.transaction():
                    yield
            finally:
                _tx_conn.reset(token)
