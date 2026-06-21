# SPDX-License-Identifier: Apache-2.0
"""Async database abstraction for ``neuron_server``.

A tiny interface over the two backends we support — **SQLite** (development) and
**PostgreSQL** (production) — so the rest of the server is written against one
small API. SQL is written with ``?`` placeholders; each backend adapts it to its
own paramstyle (PostgreSQL's ``$1``/``$2``).

SQLite uses a single connection (correct for the embedded/desktop default);
PostgreSQL uses an ``asyncpg`` pool whose size is configurable (default 1, which
matches the original serialized behaviour). Connection-affinity within a
:meth:`Database.transaction` is handled internally by each backend, so the
storage layer just passes a ``Database`` around and never sees connections.
Concrete implementations live in :mod:`neuron_server.storage.sqlite` and
:mod:`neuron_server.storage.postgres`.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

# Monotonic id streams: name -> (table, column). Each is a server-wide ascending
# counter. SQLite allocates with MAX(col)+1 (race-free under its single serialized
# connection); PostgreSQL uses a dedicated SEQUENCE so concurrent connections never
# collide. ``next_depth`` is deliberately NOT here — DAG depth is per-room and may
# legitimately repeat, so it keeps its MAX+1 computation.
STREAMS: dict[str, tuple[str, str]] = {
    "events": ("events", "stream_ordering"),
    "to_device": ("to_device_messages", "stream_id"),
    "device_lists": ("device_list_changes", "stream_id"),
    "federated_invites": ("federated_invites", "stream_id"),
    "receipts": ("receipts", "stream_id"),
    "outbox": ("federation_outbox", "stream_id"),
    # Typing rows are upserted (never deleted), so MAX(stream_id)+1 stays
    # monotonic — the serial /sync compares must never regress on "stop typing".
    "typing": ("typing", "stream_id"),
}


class Database(abc.ABC):
    """A minimal async database connection used by the storage layer."""

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the underlying connection."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close the underlying connection."""

    @abc.abstractmethod
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Run a statement that returns no rows (DDL, INSERT/UPDATE/DELETE)."""

    @abc.abstractmethod
    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run a query and return all rows as tuples."""

    @abc.abstractmethod
    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run a query and return the first column of the first row (or ``None``)."""

    @abc.abstractmethod
    def transaction(self) -> AbstractAsyncContextManager[None]:
        """An async context manager that commits on success and rolls back on error."""

    @abc.abstractmethod
    async def next_stream_id(self, name: str) -> int:
        """Allocate the next id for the ``name`` stream (see :data:`STREAMS`).

        Must be race-free across concurrent connections/transactions.
        """

    @abc.abstractmethod
    async def get_stream_position(self, name: str) -> int:
        """The safe ``/sync`` floor for the ``name`` stream.

        The highest id ``H`` such that **every** id ``<= H`` is committed — never an
        id that has been allocated but not yet committed. On multi-writer backends
        this is the minimum contiguous position across writer instances; on a
        single serialized connection it is simply ``MAX(col)`` (allocation order ==
        commit order). Replaces a raw ``MAX(col)`` watermark, which could expose an
        id committed out of allocation order and skip a lower one forever.
        """

    async def ensure_stream_sequences(self) -> None:
        """Create/seed any backend objects the stream allocator needs.

        Called once after migrations. No-op by default (SQLite needs nothing).
        """
        return None

    async def heartbeat_positions(self) -> None:
        """Periodically advance idle streams' stored positions across instances.

        Multi-writer backends call this on an interval so an idle (or crashed)
        instance's stale position row stops holding the shared ``/sync`` floor back.
        No-op by default — SQLite is a single instance with no shared floor.
        """
        return None

    @asynccontextmanager
    async def startup_lock(self) -> AsyncIterator[None]:
        """Serialize cross-process startup (migrations + sequence seeding).

        Several startup steps are unsafe to run concurrently from two workers
        against one database — non-idempotent ``ALTER TABLE`` DDL and the
        ``schema_migrations`` bookkeeping insert (duplicate PK), plus the sequence
        seed's check-then-set. Backends that support multiple processes hold a
        cross-process lock here; the default (SQLite, single process) is a no-op.
        """
        yield


def connect_database(
    url: str, *, pool_size: int = 1, instance_name: str = "master"
) -> Database:
    """Build (but do not yet connect) a :class:`Database` for the given URL.

    Supports ``sqlite:///...`` and ``postgresql://...`` / ``postgres://...``.
    The driver is imported lazily by the concrete class, so only the backend you
    actually use needs its driver installed. ``pool_size`` and ``instance_name``
    apply to PostgreSQL only (SQLite is always a single serialized connection, so
    it has one implicit instance and no in-flight id tracking).
    """
    if url.startswith("sqlite"):
        from neuron_server.storage.sqlite import SQLiteDatabase

        return SQLiteDatabase(_sqlite_path(url))
    if url.startswith(("postgresql://", "postgres://")):
        from neuron_server.storage.postgres import PostgresDatabase

        return PostgresDatabase(url, pool_size=pool_size, instance_name=instance_name)
    raise ValueError(f"Unsupported database URL: {url!r}")


def _sqlite_path(url: str) -> str:
    """Turn a ``sqlite:///...`` URL into a path aiosqlite understands.

    ``sqlite:///:memory:`` and ``sqlite://`` -> ``:memory:``;
    ``sqlite:///rel/path.db`` -> ``rel/path.db``;
    ``sqlite:////abs/path.db`` -> ``/abs/path.db``.
    """
    rest = url[len("sqlite://") :]
    if rest.startswith("/"):
        rest = rest[1:]
    if rest in ("", ":memory:"):
        return ":memory:"
    return rest
