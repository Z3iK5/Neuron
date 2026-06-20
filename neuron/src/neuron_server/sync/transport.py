# SPDX-License-Identifier: Apache-2.0
"""Cross-process wake transports for the notifier.

A :class:`NotifierTransport` carries a bare "something changed" ping between
worker processes. The only payload that matters for correctness is *that* a wake
happened — ``/sync`` re-reads every stream from the shared database — so the ping
is empty.

:class:`PgListenTransport` uses PostgreSQL ``LISTEN/NOTIFY``: it needs no new
dependency (``asyncpg`` already backs the production database) and ``NOTIFY``
fires on commit, matching the commit-then-notify ordering producers already rely
on. The ``LISTEN`` side holds a **dedicated** long-lived connection opened
*outside* the pool, so it can block on incoming notifications without starving
the (default size 1) query pool.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from neuron_core import get_logger

if TYPE_CHECKING:
    from neuron_server.storage.postgres import PostgresDatabase

_logger = get_logger(__name__)

# The single channel every worker LISTENs on / NOTIFYs. Empty payload: the wake
# is a level-trigger that tells parked syncs to re-read the DB.
_WAKE_CHANNEL = "neuron_wake"
_MAX_RECONNECT_BACKOFF_S = 30.0
# How long start() waits for the first subscription before serving anyway (so a
# slow/unreachable Postgres degrades to timeout-polling instead of hanging boot).
_CONNECT_TIMEOUT_S = 10.0
# Liveness-probe interval on the idle LISTEN connection. asyncpg sets no TCP
# keepalive, so a silently-dropped socket (managed/firewalled PG that sends no
# FIN/RST) would otherwise go undetected for hours; a periodic ``SELECT 1`` surfaces
# the dead connection promptly and triggers the reconnect+catch-up path.
_HEARTBEAT_S = 30.0


class NotifierTransport(Protocol):
    """Carries cross-process wake pings."""

    async def start(self, on_ping: Callable[[], None]) -> None:
        """Begin receiving pings; ``on_ping`` is called once per received wake."""

    async def publish(self) -> None:
        """Send a wake ping to every worker (including, harmlessly, this one)."""

    async def stop(self) -> None:
        """Stop receiving and release any held resources."""


class PgListenTransport:
    """Postgres ``LISTEN/NOTIFY`` wake transport on a dedicated connection."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db
        self._on_ping: Callable[[], None] | None = None
        self._conn: Any = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._ready: asyncio.Event | None = None

    async def start(self, on_ping: Callable[[], None]) -> None:
        self._on_ping = on_ping
        self._closing = False
        if self._task is None:
            self._ready = asyncio.Event()
            self._task = asyncio.create_task(self._run())
            # Don't begin serving until the channel is actually subscribed, so a
            # wake published right after startup isn't lost. Bounded, so a slow
            # Postgres degrades to timeout-polling rather than hanging boot.
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=_CONNECT_TIMEOUT_S)
            except TimeoutError:
                _logger.warning(
                    "pg listen transport not subscribed within %ss; serving anyway",
                    _CONNECT_TIMEOUT_S,
                )

    async def publish(self) -> None:
        # Runs on a pooled connection, after the producer's transaction has
        # committed (notify() is called post-commit), so listeners that wake see
        # the new rows. pg_notify keeps the channel name a bound value.
        await self._db.execute("SELECT pg_notify(?, ?)", (_WAKE_CHANNEL, ""))

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_conn()

    def _handle_notification(self, _conn: Any, _pid: int, _channel: str, _payload: str) -> None:
        if self._on_ping is not None:
            self._on_ping()

    async def _run(self) -> None:
        """Maintain the LISTEN connection, reconnecting with backoff if it drops."""
        backoff = 1.0
        while not self._closing:
            try:
                self._conn = await self._db.acquire_listener()
                await self._conn.add_listener(_WAKE_CHANNEL, self._handle_notification)
                backoff = 1.0
                if self._ready is not None:
                    self._ready.set()  # unblock start() once actually subscribed
                # A (re)connect may have missed notifications while we were down;
                # wake local waiters so they re-read the DB immediately.
                if self._on_ping is not None:
                    self._on_ping()
                await self._wait_until_closed(self._conn)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("pg listen transport connection error")
            finally:
                await self._close_conn()
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_RECONNECT_BACKOFF_S)

    async def _wait_until_closed(self, conn: Any) -> None:
        """Block (delivering notifications) until ``conn`` terminates or we stop.

        asyncpg dispatches LISTEN callbacks while the loop runs, so awaiting keeps
        notifications flowing. We wake every ``_HEARTBEAT_S`` to probe the socket
        with ``SELECT 1``: asyncpg's termination listener only fires on a clean
        close, so a silently-dropped connection is otherwise invisible. A failed
        probe (or a clean close) raises/returns and triggers the reconnect loop.
        """
        loop = asyncio.get_running_loop()
        closed: asyncio.Future[None] = loop.create_future()

        def _on_terminate(_c: Any) -> None:
            if not closed.done():
                closed.set_result(None)

        conn.add_termination_listener(_on_terminate)
        try:
            while not self._closing:
                try:
                    # shield: a heartbeat timeout must not cancel the shared future.
                    await asyncio.wait_for(asyncio.shield(closed), timeout=_HEARTBEAT_S)
                    return  # the connection was closed by the server
                except TimeoutError:
                    await conn.execute("SELECT 1")  # raises if the socket is dead
        finally:
            conn.remove_termination_listener(_on_terminate)

    async def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # pragma: no cover - best-effort teardown
                _logger.debug("error closing pg listen connection", exc_info=True)
