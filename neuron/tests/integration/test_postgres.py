# SPDX-License-Identifier: Apache-2.0
"""PostgreSQL backend integration tests.

Skipped unless ``NEURON_TEST_DATABASE_URL`` points at a Postgres instance (the CI
``postgres`` job sets it). These exercise the real ``asyncpg`` pool and the
sequence-based id allocator — including **concurrent** writes across pool
connections, which the old ``MAX(col)+1`` allocation would collide on.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

import httpx
import pytest

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.storage.database import connect_database
from neuron_server.storage.migrations import MIGRATIONS, run_migrations

_PG = os.environ.get("NEURON_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _PG.startswith(("postgresql://", "postgres://")),
    reason="set NEURON_TEST_DATABASE_URL to a Postgres URL to run",
)

_CS = "/_matrix/client/v3"


async def _reset_schema() -> None:
    import asyncpg

    conn = await asyncpg.connect(_PG)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    finally:
        await conn.close()


@contextlib.asynccontextmanager
async def _pg_app(pool_size: int = 8) -> AsyncIterator[httpx.AsyncClient]:
    await _reset_schema()  # fresh schema so each test seeds sequences from scratch
    app = create_app(
        NeuronServerSettings(
            name="pg.test", database_url=_PG, db_pool_size=pool_size, first_user_admin=True
        )
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://pg.test"
        ) as client:
            yield client


async def _register(c: httpx.AsyncClient, username: str) -> str:
    sess = (
        await c.post(f"{_CS}/register", json={"username": username, "password": "pw-123456"})
    ).json()["session"]
    return (
        await c.post(
            f"{_CS}/register",
            json={
                "username": username,
                "password": "pw-123456",
                "auth": {"type": "m.login.dummy", "session": sess},
            },
        )
    ).json()["access_token"]


async def test_concurrent_worker_startup_is_safe() -> None:
    """Two workers starting against one fresh DB must not crash on a duplicate
    schema_migrations PK or a non-idempotent ALTER TABLE — the startup advisory
    lock serializes them, and each migration is recorded exactly once."""
    await _reset_schema()

    async def start_worker() -> None:
        db = connect_database(_PG, pool_size=4)
        await db.connect()
        try:
            async with db.startup_lock():
                await run_migrations(db)
                await db.ensure_stream_sequences()
        finally:
            await db.disconnect()

    # Run both startups concurrently; without the lock the racing ALTER TABLEs /
    # duplicate-PK insert would raise.
    await asyncio.gather(start_worker(), start_worker())

    check = connect_database(_PG)
    await check.connect()
    try:
        count = await check.fetchval("SELECT COUNT(*) FROM schema_migrations")
        distinct = await check.fetchval("SELECT COUNT(DISTINCT version) FROM schema_migrations")
    finally:
        await check.disconnect()
    assert count == len(MIGRATIONS)
    assert distinct == len(MIGRATIONS)


async def test_core_flow_on_postgres() -> None:
    async with _pg_app() as c:
        h = {"Authorization": f"Bearer {await _register(c, 'admin')}"}
        room = (
            await c.post(f"{_CS}/createRoom", headers=h, json={"preset": "public_chat"})
        ).json()["room_id"]
        sent = await c.put(
            f"{_CS}/rooms/{room}/send/m.room.message/t1",
            headers=h,
            json={"msgtype": "m.text", "body": "hi"},
        )
        assert sent.status_code == 200 and sent.json()["event_id"].startswith("$")
        sync = (await c.get(f"{_CS}/sync", headers=h)).json()
        assert room in sync["rooms"]["join"]
        ver = (await c.get("/_synapse/admin/v1/server_version", headers=h)).json()
        assert ver["server_version"].startswith("Neuron ")


@contextlib.asynccontextmanager
async def _two_workers(
    pool_size: int = 4,
) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    """Two app instances sharing one Postgres DB — a faithful two-worker setup.

    Each gets its own ``BroadcastNotifier`` and dedicated ``LISTEN`` connection, so
    a wake published by one must reach a ``/sync`` parked on the other only via
    real Postgres ``NOTIFY`` (not any in-process shortcut).
    """
    await _reset_schema()

    def _mk() -> object:
        return create_app(
            NeuronServerSettings(
                name="pg.test", database_url=_PG, db_pool_size=pool_size, first_user_admin=True
            )
        )

    app_a, app_b = _mk(), _mk()
    async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_a), base_url="http://a.pg.test"
            ) as a,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_b), base_url="http://b.pg.test"
            ) as b,
        ):
            yield a, b


def _typing_users(sync_json: dict, room_id: str) -> list[str]:
    room = sync_json["rooms"]["join"].get(room_id, {})
    for event in room.get("ephemeral", {}).get("events", []):
        if event["type"] == "m.typing":
            return event["content"].get("user_ids", [])
    return []


async def test_cross_worker_sync_wakeup() -> None:
    """A send on worker A must wake a /sync long-poll parked on worker B, via
    Postgres LISTEN/NOTIFY — otherwise B only returns when its own timeout fires."""
    async with _two_workers() as (a, b):
        h = {"Authorization": f"Bearer {await _register(a, 'alice')}"}
        room = (
            await a.post(f"{_CS}/createRoom", headers=h, json={"preset": "public_chat"})
        ).json()["room_id"]
        since = (await b.get(f"{_CS}/sync?timeout=0", headers=h)).json()["next_batch"]

        # Park a 30s long-poll on B, then send from A. With cross-worker wakeup it
        # returns near-instantly; without it, it would block the full 30s (so the
        # 10s wait_for below would time out and fail the test).
        sync_task = asyncio.create_task(
            b.get(f"{_CS}/sync?since={since}&timeout=30000", headers=h)
        )
        await asyncio.sleep(0.3)
        sent = await a.put(
            f"{_CS}/rooms/{room}/send/m.room.message/t1",
            headers=h,
            json={"msgtype": "m.text", "body": "ping"},
        )
        assert sent.status_code == 200
        resp = await asyncio.wait_for(sync_task, timeout=10)

        body = resp.json()
        assert room in body["rooms"]["join"]
        bodies = [
            e.get("content", {}).get("body")
            for e in body["rooms"]["join"][room]["timeline"]["events"]
        ]
        assert "ping" in bodies


async def test_cross_worker_typing_visible() -> None:
    """Typing set on worker A is visible to a /sync on worker B (DB-backed typing)."""
    async with _two_workers() as (a, b):
        h = {"Authorization": f"Bearer {await _register(a, 'alice')}"}
        room = (
            await a.post(f"{_CS}/createRoom", headers=h, json={"preset": "public_chat"})
        ).json()["room_id"]

        await a.put(
            f"{_CS}/rooms/{room}/typing/@alice:pg.test",
            headers=h,
            json={"typing": True, "timeout": 30000},
        )
        sync = (await b.get(f"{_CS}/sync", headers=h)).json()
        assert "@alice:pg.test" in _typing_users(sync, room)

        # Stopping on A clears it for B too.
        await a.put(
            f"{_CS}/rooms/{room}/typing/@alice:pg.test",
            headers=h,
            json={"typing": False},
        )
        sync2 = (await b.get(f"{_CS}/sync", headers=h)).json()
        assert "@alice:pg.test" not in _typing_users(sync2, room)


async def test_concurrent_sends_get_distinct_stream_ids() -> None:
    """Concurrent transactions across pool connections must each get a distinct
    stream id from the sequence; the old MAX+1 allocation collided on the unique
    stream-ordering index here."""
    async with _pg_app(pool_size=8) as c:
        h = {"Authorization": f"Bearer {await _register(c, 'admin')}"}
        room = (
            await c.post(f"{_CS}/createRoom", headers=h, json={"preset": "public_chat"})
        ).json()["room_id"]

        n = 25

        async def send(i: int) -> httpx.Response:
            return await c.put(
                f"{_CS}/rooms/{room}/send/m.room.message/c{i}",
                headers=h,
                json={"msgtype": "m.text", "body": f"m{i}"},
            )

        results = await asyncio.gather(*(send(i) for i in range(n)))
        assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
        # All distinct event ids — nothing lost to a stream-id collision.
        assert len({r.json()["event_id"] for r in results}) == n
        msgs = (
            await c.get(f"{_CS}/rooms/{room}/messages?dir=b&limit=50", headers=h)
        ).json()["chunk"]
        assert len([m for m in msgs if m["type"] == "m.room.message"]) == n
