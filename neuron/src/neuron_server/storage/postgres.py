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

The pool size defaults to **1** but may now be raised **within a single process**:
a per-stream in-flight tracker (:class:`_StreamTracker`) records each writer's
contiguous "persisted upto" position in ``stream_positions``, and ``/sync`` reads
that floor via :meth:`get_stream_position` instead of ``MAX(col)``. So an id
allocated before — but committed after — a higher one holds the floor back until it
commits, and is never skipped (the multi-writer lost-event gap). Running multiple
worker *processes* (distinct ``instance_name``s) is loss-free too, but an idle
instance's stored position can lag the true floor until a position heartbeat lands
(follow-up) — so prefer a single process with a larger pool for now.
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from neuron_core import get_logger
from neuron_server.storage.database import STREAMS, Database

_logger = get_logger(__name__)

# Fixed key for the startup advisory lock (ascii "neuron"). pg_advisory_lock takes
# a single bigint; we use exactly this one lock, so any constant is fine.
_STARTUP_LOCK_KEY = 0x6E6575726F6E

# The connection pinned to the current transaction (None when not in one). A
# context variable rather than instance state, so concurrent tasks each see only
# their own transaction's connection.
_tx_conn: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "neuron_pg_tx_conn", default=None
)

# Ids allocated in the current transaction, as (stream_name, id) — drained when the
# transaction finishes so each id is marked done in its stream's tracker. ``None``
# when not in a transaction (a bare nextval, e.g. typing, is finished immediately).
_tx_pending: contextvars.ContextVar[list[tuple[str, int]] | None] = contextvars.ContextVar(
    "neuron_pg_tx_pending", default=None
)


class _StreamTracker:
    """Tracks a stream's contiguous "persisted upto" position for one writer.

    Holds the set of ids that have been allocated but not yet committed/rolled
    back. The position is the highest id with no in-flight id at or below it:
    ``min(in_flight) - 1`` while anything is in flight, else the highest id ever
    seen. So an id allocated before — but committed after — a higher id holds the
    position back until it finishes, which is exactly what keeps /sync from
    advancing past a not-yet-committed row.
    """

    __slots__ = ("_in_flight", "_max_seen")

    def __init__(self, initial: int) -> None:
        self._in_flight: set[int] = set()
        self._max_seen = initial

    def allocate(self, stream_id: int) -> None:
        self._in_flight.add(stream_id)
        if stream_id > self._max_seen:
            self._max_seen = stream_id

    def finish(self, stream_id: int) -> None:
        # Discard on commit AND on rollback: a burned (rolled-back) id is a
        # permanent hole in the sequence and must not stall the position forever.
        self._in_flight.discard(stream_id)

    def position(self) -> int:
        if self._in_flight:
            return min(self._in_flight) - 1
        return self._max_seen


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

    def __init__(self, dsn: str, *, pool_size: int = 1, instance_name: str = "master") -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._pool_size = max(1, pool_size)
        self._instance_name = instance_name
        # Per-stream in-flight id trackers, seeded in ensure_stream_sequences.
        self._trackers: dict[str, _StreamTracker] = {}
        # Streams whose last position flush failed; retried on the next transaction
        # so a transient error doesn't strand a committed position on an idle stream.
        self._needs_reflush: set[str] = set()

    async def connect(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=self._pool_size
        )

    async def disconnect(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def acquire_listener(self) -> Any:
        """Open a dedicated connection *outside* the pool, for ``LISTEN``.

        A ``LISTEN`` connection blocks waiting for notifications, so it must not be
        one of the pool's connections (with the default ``pool_size=1`` that would
        starve every query). The caller owns this connection and must close it.
        """
        import asyncpg

        return await asyncpg.connect(self._dsn)

    @asynccontextmanager
    async def startup_lock(self) -> AsyncIterator[None]:
        """Hold a session-scoped advisory lock so only one worker runs startup.

        Session-scoped (``pg_advisory_lock``), not transaction-scoped, because
        :func:`run_migrations` opens its own per-migration transactions inside this
        block — an ``xact`` lock could not span them. Held on a dedicated
        connection (outside the pool) for the duration, released in ``finally``.
        """
        import asyncpg

        conn = await asyncpg.connect(self._dsn)
        try:
            await conn.execute("SELECT pg_advisory_lock($1)", _STARTUP_LOCK_KEY)
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", _STARTUP_LOCK_KEY)
        finally:
            await conn.close()

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
                # Seed this instance's in-flight tracker and persisted-upto row at the
                # current data height: with no in-flight ids yet, everything that
                # exists is committed, so the floor starts there (never spuriously 0).
                # GREATEST keeps a restart from regressing an already-higher row.
                max_id = int(await conn.fetchval(f"SELECT COALESCE(MAX({col}), 0) FROM {table}"))
                self._trackers[name] = _StreamTracker(max_id)
                await conn.execute(
                    "INSERT INTO stream_positions (stream_name, instance_name, stream_id)"
                    " VALUES ($1, $2, $3)"
                    " ON CONFLICT (stream_name, instance_name) DO UPDATE SET"
                    " stream_id = GREATEST(stream_positions.stream_id, EXCLUDED.stream_id)",
                    name,
                    self._instance_name,
                    max_id,
                )

    async def next_stream_id(self, name: str) -> int:
        # nextval is non-transactional: concurrent connections get distinct ids
        # with no lock held for the duration of the caller's transaction.
        stream_id = int(await self.fetchval(f"SELECT nextval('{self._seq(name)}')"))
        tracker = self._trackers.get(name)
        pending = _tx_pending.get()
        if tracker is not None and pending is not None:
            # Only ids allocated INSIDE a transaction move the floor: they insert a
            # tracked row, so the tracker holds the position back until they commit
            # and advances it on finish. A bare nextval outside a transaction (typing,
            # or a read-only stream-position cutoff like federation backfill /
            # backward pagination) inserts no row for this id — tracking it would
            # bump the floor to a phantom id — so it only burns a sequence value.
            tracker.allocate(stream_id)
            pending.append((name, stream_id))
        return stream_id

    async def get_stream_position(self, name: str) -> int:
        # The safe floor is the minimum contiguous position across writer instances:
        # any instance with a low in-flight id keeps its row (and so the MIN) back
        # until that id commits, so the floor never exceeds the contiguous-committed
        # id. Seeded for every stream at startup, so it is never spuriously 0.
        return int(
            await self.fetchval(
                "SELECT COALESCE(MIN(stream_id), 0) FROM stream_positions"
                " WHERE stream_name = ?",
                (name,),
            )
        )

    async def _flush_position(self, conn: Any, name: str) -> None:
        """Upsert this instance's current position for ``name`` (never regressing)."""
        position = self._trackers[name].position()
        await conn.execute(
            "INSERT INTO stream_positions (stream_name, instance_name, stream_id)"
            " VALUES ($1, $2, $3)"
            " ON CONFLICT (stream_name, instance_name) DO UPDATE SET"
            " stream_id = GREATEST(stream_positions.stream_id, EXCLUDED.stream_id)",
            name,
            self._instance_name,
            position,
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        pinned = _tx_conn.get()
        if pinned is not None:
            # Already in a transaction on this task — nest via a savepoint on the
            # same connection. Ids allocated here append to the outer transaction's
            # pending list (the contextvar still points at it), so the outer block
            # finishes them.
            async with pinned.transaction():
                yield
            return
        async with self._pool.acquire() as conn:
            ctoken = _tx_conn.set(conn)
            pending: list[tuple[str, int]] = []
            ptoken = _tx_pending.set(pending)
            try:
                async with conn.transaction():
                    yield
            finally:
                _tx_conn.reset(ctoken)
                _tx_pending.reset(ptoken)
                # Mark every id allocated in this transaction done — on commit AND
                # on rollback (a burned id must not stall the contiguous position).
                affected: set[str] = set()
                for name, stream_id in pending:
                    tracker = self._trackers.get(name)
                    if tracker is not None:
                        tracker.finish(stream_id)
                        affected.add(name)
                # Flush updated positions on the same connection, after the inner
                # transaction has committed/rolled back — so a reader never sees a
                # position ahead of committed rows. Also retry any stream whose
                # previous flush failed, so a transient error doesn't strand a
                # committed position until the next write to that same (perhaps idle)
                # stream. A failure only ever leaves the floor behind, never ahead.
                for name in affected | self._needs_reflush:
                    try:
                        await self._flush_position(conn, name)
                        self._needs_reflush.discard(name)
                    except Exception:
                        self._needs_reflush.add(name)
                        _logger.warning(
                            "failed to flush stream position for %s; will retry", name,
                            exc_info=True,
                        )
