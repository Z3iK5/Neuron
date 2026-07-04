# SPDX-License-Identifier: Apache-2.0
"""Wake-up primitives for long-polling ``/sync``.

When something a client cares about changes, a *notify* wakes every ``/sync``
that is currently waiting; a waiter blocks (up to a timeout) until woken, then
re-checks the database. ``/sync`` re-reads every stream's position from the DB on
each build, so the wake carries **no payload** — it is a bare "something changed"
ping, and over-waking is harmless (just an extra DB build).

Two implementations satisfy the :class:`Notifier` seam:

- :class:`StreamNotifier` — in-process ``asyncio`` futures. Correct (and the only
  thing needed) for the single-process desktop/SQLite default. Zero dependencies.
- :class:`~neuron_server.sync.broadcast.BroadcastNotifier` — wraps a local
  ``StreamNotifier`` with a cross-process transport (Postgres ``LISTEN/NOTIFY``),
  so a wake from one worker reaches ``/sync`` calls parked on *other* workers.
  Selected automatically for PostgreSQL deployments by :func:`build_notifier`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from neuron_server.config import NeuronServerSettings
    from neuron_server.storage.database import Database


@runtime_checkable
class Notifier(Protocol):
    """The wake-up seam every ``/sync`` producer and the sync service depend on.

    ``notify`` must stay a **zero-arg, synchronous, fire-and-forget** callable: it
    is bound as ``app.state.notify`` and passed as the ``notify=`` kwarg to
    ``RoomService``/``E2EEService``/``FederatedMembership``/``TypingHandler``, so
    its shape is load-bearing across ~18 call sites. ``start``/``stop`` manage any
    background transport (no-ops for the in-process impl).
    """

    def notify(self) -> None: ...
    async def wait(self, timeout_seconds: float) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class StreamNotifier:
    """Wakes waiting ``/sync`` requests when new events arrive (single process)."""

    def __init__(self) -> None:
        self._waiters: list[asyncio.Future[None]] = []

    def notify(self) -> None:
        """Wake all currently-waiting syncs."""
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters = []

    async def wait(self, timeout_seconds: float) -> None:
        """Block until the next :meth:`notify` or until ``timeout_seconds`` elapse."""
        loop = asyncio.get_event_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout_seconds)
        except TimeoutError:
            pass
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    async def start(self) -> None:
        """No-op: the in-process notifier has no background transport."""

    async def stop(self) -> None:
        """No-op: the in-process notifier has no background transport."""


def build_notifier(settings: NeuronServerSettings, db: Database) -> Notifier:
    """Select the notifier backend for this deployment.

    Mirrors :func:`~neuron_server.storage.database.connect_database`: dispatches on
    the database URL scheme and **lazily imports** the cross-process pieces, so the
    desktop/SQLite path never pulls in the Postgres transport. ``db`` must already
    be connected (the Postgres transport opens a dedicated ``LISTEN`` connection).

    - ``inprocess`` (or any SQLite URL under ``auto``) -> :class:`StreamNotifier`.
    - ``pg`` / ``auto`` on a ``postgresql://`` URL -> a ``BroadcastNotifier`` over
      Postgres ``LISTEN/NOTIFY`` (no new dependency — ``asyncpg`` is already used).
    """
    backend = settings.notifier_backend
    is_pg = settings.database_url.startswith(("postgresql://", "postgres://"))

    if backend == "inprocess":
        return StreamNotifier()
    if backend == "pg" and not is_pg:
        raise ValueError("notifier_backend='pg' requires a postgresql:// database_url")
    if backend not in ("auto", "pg"):
        raise ValueError(f"unknown notifier_backend: {backend!r}")

    # auto + sqlite, or any non-pg under auto: stay in-process.
    if not is_pg:
        return StreamNotifier()

    # Postgres: wake across workers via LISTEN/NOTIFY on a dedicated connection.
    from neuron_server.storage.postgres import PostgresDatabase
    from neuron_server.sync.broadcast import BroadcastNotifier
    from neuron_server.sync.transport import PgListenTransport

    if not isinstance(db, PostgresDatabase):  # pragma: no cover - defensive
        raise TypeError("Postgres notifier requires a PostgresDatabase")
    return BroadcastNotifier(StreamNotifier(), PgListenTransport(db))
