# SPDX-License-Identifier: Apache-2.0
"""Async database abstraction for ``neuron_server``.

A tiny interface over the two backends we support — **SQLite** (development) and
**PostgreSQL** (production) — so the rest of the server is written against one
small API. SQL is written with ``?`` placeholders; each backend adapts it to its
own paramstyle (PostgreSQL's ``$1``/``$2``).

This is deliberately minimal for the HS-0 foundation: a single connection per
``Database`` (connection pooling is a later performance concern, HS-8). Concrete
implementations live in :mod:`neuron_server.storage.sqlite` and
:mod:`neuron_server.storage.postgres`.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any


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


def connect_database(url: str) -> Database:
    """Build (but do not yet connect) a :class:`Database` for the given URL.

    Supports ``sqlite:///...`` and ``postgresql://...`` / ``postgres://...``.
    The driver is imported lazily by the concrete class, so only the backend you
    actually use needs its driver installed.
    """
    if url.startswith("sqlite"):
        from neuron_server.storage.sqlite import SQLiteDatabase

        return SQLiteDatabase(_sqlite_path(url))
    if url.startswith(("postgresql://", "postgres://")):
        from neuron_server.storage.postgres import PostgresDatabase

        return PostgresDatabase(url)
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
