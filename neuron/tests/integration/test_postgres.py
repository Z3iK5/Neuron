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
            name="pg.test",
            database_url=_PG,
            db_pool_size=pool_size,
            first_user_admin=True,
            rate_limit_enabled=False,  # these exercise storage concurrency, not limits
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


async def _migrate(db: object) -> None:
    async with db.startup_lock():  # type: ignore[attr-defined]
        await run_migrations(db)  # type: ignore[arg-type]
        await db.ensure_stream_sequences()  # type: ignore[attr-defined]


async def test_pool_concurrent_writes_never_skip_a_position() -> None:
    """The core multi-writer fix: with >1 connection an id committed out of
    allocation order must NOT advance the /sync floor past a lower, still-in-flight
    id (the old MAX(col) watermark did, losing that event). Single instance,
    pool_size>1 — exactly the case this PR makes safe."""
    await _reset_schema()
    db = connect_database(_PG, pool_size=4)
    await db.connect()
    try:
        await _migrate(db)
        assert await db.get_stream_position("events") == 0  # fresh DB

        a_allocated = asyncio.Event()
        release_a = asyncio.Event()
        a_id: dict[str, int] = {}

        async def writer_a() -> None:
            async with db.transaction():
                a_id["v"] = await db.next_stream_id("events")
                a_allocated.set()
                await release_a.wait()  # hold the transaction open (id in-flight)

        task_a = asyncio.create_task(writer_a())
        await a_allocated.wait()

        # B allocates a higher id and commits while A is still open.
        async with db.transaction():
            b_id = await db.next_stream_id("events")
        assert b_id > a_id["v"]

        # Floor stays at A's id - 1 — b_id is committed but NOT exposed while the
        # lower id is in-flight. (MAX(col) would already report b_id here.)
        assert await db.get_stream_position("events") == a_id["v"] - 1

        # Once A commits, the floor jumps to the now-contiguous maximum.
        release_a.set()
        await task_a
        assert await db.get_stream_position("events") == b_id
    finally:
        await db.disconnect()


async def test_rolled_back_id_does_not_stall_the_floor() -> None:
    """A burned (rolled-back) sequence id is a permanent hole; it must be marked
    done so it never stalls the contiguous position forever."""
    await _reset_schema()
    db = connect_database(_PG, pool_size=4)
    await db.connect()
    try:
        await _migrate(db)
        with contextlib.suppress(RuntimeError):
            async with db.transaction():
                await db.next_stream_id("events")  # allocated...
                raise RuntimeError("boom")  # ...then rolled back (burned)
        async with db.transaction():
            sid = await db.next_stream_id("events")
        # The floor advances to the committed id, not stuck behind the burned one.
        assert await db.get_stream_position("events") == sid
    finally:
        await db.disconnect()


async def test_floor_held_across_instances() -> None:
    """Across two worker instances the floor is the MIN of their positions: an
    instance holding a low in-flight id keeps the floor back, so the other
    instance's higher committed id is not exposed prematurely (no lost event)."""
    await _reset_schema()
    db_a = connect_database(_PG, pool_size=2, instance_name="a")
    db_b = connect_database(_PG, pool_size=2, instance_name="b")
    await db_a.connect()
    await db_b.connect()
    try:
        await _migrate(db_a)
        await db_b.ensure_stream_sequences()  # instance b seeds its own position row

        a_allocated = asyncio.Event()
        release_a = asyncio.Event()
        a_id: dict[str, int] = {}

        async def writer_a() -> None:
            async with db_a.transaction():
                a_id["v"] = await db_a.next_stream_id("events")
                a_allocated.set()
                await release_a.wait()

        task_a = asyncio.create_task(writer_a())
        await a_allocated.wait()

        async with db_b.transaction():
            b_id = await db_b.next_stream_id("events")
        assert b_id > a_id["v"]

        # Read from B: the MIN-across-instances floor is held below b_id by A's
        # not-yet-committed lower id.
        assert await db_b.get_stream_position("events") < b_id
        assert await db_b.get_stream_position("events") == a_id["v"] - 1

        release_a.set()
        await task_a
    finally:
        await db_a.disconnect()
        await db_b.disconnect()


_DL_INSERT = "INSERT INTO device_list_changes (stream_id, user_id) VALUES (?, ?)"


async def test_heartbeat_advances_idle_instance_floor() -> None:
    """An idle instance pins the MIN-across-instances floor at its last position.
    The heartbeat advances an idle stream to the committed MAX, releasing the floor
    so another instance's higher committed id becomes visible to /sync. (device_lists
    is used for a minimal insertable row; the mechanism is identical for events.)"""
    await _reset_schema()
    db_a = connect_database(_PG, pool_size=2, instance_name="a")
    db_b = connect_database(_PG, pool_size=2, instance_name="b")
    await db_a.connect()
    await db_b.connect()
    try:
        await _migrate(db_a)
        await db_b.ensure_stream_sequences()
        async with db_a.transaction():
            a1 = await db_a.next_stream_id("device_lists")
            await db_a.execute(_DL_INSERT, (a1, "@a:pg.test"))
        async with db_b.transaction():
            b1 = await db_b.next_stream_id("device_lists")
            await db_b.execute(_DL_INSERT, (b1, "@b:pg.test"))
        assert b1 > a1
        # A is idle at a1; the floor is held there by A's row.
        assert await db_b.get_stream_position("device_lists") == a1
        # A's heartbeat advances its idle position to the committed MAX.
        await db_a.heartbeat_positions()
        assert await db_b.get_stream_position("device_lists") == b1
    finally:
        await db_a.disconnect()
        await db_b.disconnect()


async def test_heartbeat_does_not_advance_past_in_flight() -> None:
    """The heartbeat must never expose a not-yet-committed id: MAX(col) excludes it
    and the owning (busy) instance is skipped, so the floor stays below it."""
    await _reset_schema()
    db_a = connect_database(_PG, pool_size=2, instance_name="a")
    db_b = connect_database(_PG, pool_size=2, instance_name="b")
    await db_a.connect()
    await db_b.connect()
    try:
        await _migrate(db_a)
        await db_b.ensure_stream_sequences()
        allocated = asyncio.Event()
        release = asyncio.Event()
        a_id: dict[str, int] = {}

        async def hold_a() -> None:
            async with db_a.transaction():
                a_id["v"] = await db_a.next_stream_id("device_lists")
                await db_a.execute(_DL_INSERT, (a_id["v"], "@a:pg.test"))
                allocated.set()
                await release.wait()  # hold the row uncommitted

        task = asyncio.create_task(hold_a())
        await allocated.wait()
        # Heartbeat both instances while A's row is in flight (uncommitted).
        await db_b.heartbeat_positions()  # B idle -> MAX(col) excludes A's row
        await db_a.heartbeat_positions()  # A is busy for device_lists -> skipped
        assert await db_b.get_stream_position("device_lists") < a_id["v"]
        release.set()
        await task
    finally:
        await db_a.disconnect()
        await db_b.disconnect()


async def test_heartbeat_skips_stream_with_pending_allocation() -> None:
    """Guards the nextval->allocate TOCTOU: while an allocation is in flight (id
    consumed at the DB but not yet tracked), the heartbeat must NOT advance the
    stream to MAX(col) — a higher id committed out of order would otherwise be
    published as the floor above the in-flight id (a lost event)."""
    await _reset_schema()
    db_a = connect_database(_PG, pool_size=2, instance_name="a")
    db_b = connect_database(_PG, pool_size=2, instance_name="b")
    await db_a.connect()
    await db_b.connect()
    try:
        await _migrate(db_a)
        await db_b.ensure_stream_sequences()
        # B commits a higher row so MAX(col) exceeds A's stored position.
        async with db_b.transaction():
            sid = await db_b.next_stream_id("device_lists")
            await db_b.execute(_DL_INSERT, (sid, "@b:pg.test"))
        # Simulate A's nextval->allocate limbo: consumed-but-untracked allocation.
        db_a._trackers["device_lists"].begin_alloc()  # noqa: SLF001 - white-box test
        before = await db_a.get_stream_position("device_lists")
        await db_a.heartbeat_positions()  # must skip device_lists (busy via pending)
        assert await db_a.get_stream_position("device_lists") == before
        # Once the allocation resolves and the stream is idle, the heartbeat advances.
        db_a._trackers["device_lists"].end_alloc()  # noqa: SLF001 - white-box test
        await db_a.heartbeat_positions()
        assert await db_a.get_stream_position("device_lists") == sid
    finally:
        await db_a.disconnect()
        await db_b.disconnect()


async def test_readonly_cutoff_allocation_does_not_pollute_floor() -> None:
    """A read-only cutoff (a bare nextval outside a transaction, as federation
    backfill / backward pagination do) inserts no row, so it must not advance the
    /sync floor to a phantom id — even when a concurrent real write later flushes."""
    await _reset_schema()
    db = connect_database(_PG, pool_size=4)
    await db.connect()
    try:
        await _migrate(db)
        allocated = asyncio.Event()
        release = asyncio.Event()
        real: dict[str, int] = {}

        async def real_send() -> None:
            async with db.transaction():
                real["id"] = await db.next_stream_id("events")
                allocated.set()
                await release.wait()

        task = asyncio.create_task(real_send())
        await allocated.wait()
        # Read-only cutoff burns a HIGHER id while the real write is still open.
        phantom = await db.next_stream_id("events")
        assert phantom > real["id"]
        release.set()
        await task
        # The floor is the real committed id, not the phantom read-only allocation.
        assert await db.get_stream_position("events") == real["id"]
    finally:
        await db.disconnect()


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


async def test_account_data_stream_on_postgres() -> None:
    """Account data roams via /sync on the sequence-backed account_data stream:
    delivered on initial sync, only-changed on incremental (migration 20)."""
    async with _pg_app() as c:
        h = {"Authorization": f"Bearer {await _register(c, 'admin')}"}
        await c.put(
            f"{_CS}/user/@admin:pg.test/account_data/m.test",
            headers=h,
            json={"colour": "blue"},
        )
        initial = (await c.get(f"{_CS}/sync?timeout=0", headers=h)).json()
        assert {"type": "m.test", "content": {"colour": "blue"}} in initial[
            "account_data"
        ]["events"]

        since = initial["next_batch"]
        empty = (await c.get(f"{_CS}/sync?since={since}&timeout=0", headers=h)).json()
        assert empty["account_data"]["events"] == []

        await c.put(
            f"{_CS}/user/@admin:pg.test/account_data/m.test",
            headers=h,
            json={"colour": "red"},
        )
        inc = (await c.get(f"{_CS}/sync?since={since}&timeout=0", headers=h)).json()
        assert inc["account_data"]["events"] == [
            {"type": "m.test", "content": {"colour": "red"}}
        ]


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
                name="pg.test",
                database_url=_PG,
                db_pool_size=pool_size,
                first_user_admin=True,
                rate_limit_enabled=False,
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


async def test_uploader_like_escape_on_postgres() -> None:
    """The media uploader filter's `LIKE ? ESCAPE '\\'` + placeholder translation
    must work on real Postgres/asyncpg, not just SQLite."""
    from neuron_server.storage import media as media_store

    await _reset_schema()
    db = connect_database(_PG, pool_size=2)
    await db.connect()
    try:
        await run_migrations(db)
        await media_store.create_media(db, "m1", "text/plain", None, 1, "@a_b:pg.test", 1000)
        await media_store.create_media(db, "m2", "text/plain", None, 1, "@axb:pg.test", 1000)
        # '_' must be literal (escaped), so 'axb' is NOT matched.
        assert await media_store.count_media(db, uploader="a_b") == 1
        rows = await media_store.list_media(db, offset=0, limit=10, uploader="a_b")
        assert [m.uploader for m in rows] == ["@a_b:pg.test"]
        # '%' must not match everything.
        assert await media_store.count_media(db, uploader="%") == 0
    finally:
        await db.disconnect()


async def test_room_key_backup_on_postgres() -> None:
    """Migration 23 + the keybackup storage module on real Postgres/asyncpg:
    version lifecycle, the replacement algorithm's compare-and-upsert (including
    RETURNING-based deletes), and etag bumps."""
    from neuron_server.storage import keybackup

    await _reset_schema()
    db = connect_database(_PG, pool_size=2)
    await db.connect()
    try:
        await run_migrations(db)
        user = "@alice:pg.test"
        v1 = await keybackup.create_version(db, user, "m.megolm_backup.v1", {"public_key": "pk"})
        assert v1 == 1

        key = {
            "first_message_index": 5,
            "forwarded_count": 0,
            "is_verified": False,
            "session_data": {"ciphertext": "original"},
        }
        assert await keybackup.put_keys(db, user, v1, {"!r:pg.test": {"sessions": {"s1": key}}})
        # A worse key (higher first_message_index) is not stored and bumps nothing.
        worse = dict(key, first_message_index=9, session_data={"ciphertext": "worse"})
        assert not await keybackup.put_keys(
            db, user, v1, {"!r:pg.test": {"sessions": {"s1": worse}}}
        )
        info = await keybackup.get_version(db, user)
        assert info is not None and info.etag == 1
        stored = await keybackup.get_keys(db, user, v1)
        assert stored["!r:pg.test"]["sessions"]["s1"]["session_data"] == {
            "ciphertext": "original"
        }

        # Delete bumps the etag; soft-deleting the version drops its keys but
        # keeps numbering monotonic.
        assert await keybackup.delete_keys(db, user, v1, "!r:pg.test", "s1")
        info = await keybackup.get_version(db, user)
        assert info is not None and info.etag == 2
        await keybackup.delete_version(db, user, v1)
        assert await keybackup.get_version(db, user) is None
        assert await keybackup.create_version(db, user, "m.megolm_backup.v1", {}) == 2
    finally:
        await db.disconnect()


async def test_remote_media_cache_on_postgres() -> None:
    """Migration 24 + the remote_media storage module on real Postgres/asyncpg:
    round-trip a cache row and confirm the INSERT ... ON CONFLICT DO NOTHING keeps a
    concurrent duplicate fetch idempotent (the first write wins, the second no-ops)."""
    from neuron_server.storage import remote_media

    await _reset_schema()
    db = connect_database(_PG, pool_size=2)
    await db.connect()
    try:
        await run_migrations(db)
        assert await remote_media.get_remote_media(db, "a.test", "m1") is None
        await remote_media.create_remote_media(
            db, "a.test", "m1", "remote_abc", "image/png", "pic.png", 42, 1000
        )
        row = await remote_media.get_remote_media(db, "a.test", "m1")
        assert row is not None and row.cache_key == "remote_abc" and row.size == 42
        # A duplicate (origin_server, origin_media_id) is a no-op, not an error.
        await remote_media.create_remote_media(
            db, "a.test", "m1", "remote_xyz", "image/png", None, 99, 2000
        )
        row = await remote_media.get_remote_media(db, "a.test", "m1")
        assert row is not None and row.cache_key == "remote_abc"  # unchanged
    finally:
        await db.disconnect()


async def test_refresh_tokens_on_postgres() -> None:
    """Migration 27 + the refresh-token storage on real Postgres/asyncpg: an access
    token with a set `expires_at_ms` round-trips, and the single-use refresh-token
    rotation (consume marks it used + links the successor) behaves as on SQLite."""
    from neuron_server.storage import accounts

    await _reset_schema()
    db = connect_database(_PG, pool_size=2)
    await db.connect()
    try:
        await run_migrations(db)
        user, device = "@alice:pg.test", "DEV"
        await accounts.create_user(db, user, None, False, 1000)
        await accounts.create_device(db, user, device, "phone", 1000)

        # Access token with an expiry round-trips (NULL vs set both exercised).
        await accounts.create_access_token(db, "at-exp", user, device, 1000, 5000)
        await accounts.create_access_token(db, "at-classic", user, device, 1000, None)
        assert await accounts.get_token(db, "at-exp") == (user, device, 5000)
        assert await accounts.get_token(db, "at-classic") == (user, device, None)

        # Refresh token: create -> get -> consume(rotate) -> replay is used.
        await accounts.create_refresh_token(db, "rt-1", user, device, 1000)
        row = await accounts.get_refresh_token(db, "rt-1")
        assert row is not None and row.user_id == user and row.used is False
        await accounts.consume_refresh_token(db, "rt-1", "rt-2")
        spent = await accounts.get_refresh_token(db, "rt-1")
        assert spent is not None and spent.used is True and spent.next_token == "rt-2"

        # delete_tokens_for_device clears both access and refresh tokens.
        await accounts.create_refresh_token(db, "rt-2", user, device, 2000)
        await accounts.delete_tokens_for_device(db, user, device)
        assert await accounts.get_token(db, "at-exp") is None
        assert await accounts.get_refresh_token(db, "rt-2") is None
    finally:
        await db.disconnect()


async def test_room_directory_on_postgres() -> None:
    """Migration 25 + the directory storage module on real Postgres/asyncpg: alias
    create is unique (a duplicate does not overwrite), resolve/delete round-trip, and
    the published-flag upsert / list-public query behave the same as on SQLite."""
    from neuron_server.storage import directory

    await _reset_schema()
    db = connect_database(_PG, pool_size=2)
    await db.connect()
    try:
        await run_migrations(db)
        alias = "#general:pg.test"
        assert await directory.resolve_alias(db, alias) is None
        assert await directory.create_alias(db, alias, "!r1:pg.test", "@a:pg.test", 1000)
        # A duplicate alias is refused and does not overwrite the mapping.
        assert not await directory.create_alias(db, alias, "!r2:pg.test", "@b:pg.test", 2000)
        assert await directory.resolve_alias(db, alias) == "!r1:pg.test"
        assert await directory.get_alias_creator(db, alias) == "@a:pg.test"
        assert await directory.aliases_for_room(db, "!r1:pg.test") == [alias]

        # Published flag: default private, upsert to public, then list.
        assert await directory.get_visibility(db, "!r1:pg.test") == "private"
        await directory.set_visibility(db, "!r1:pg.test", "public")
        await directory.set_visibility(db, "!r1:pg.test", "public")  # upsert is idempotent
        assert await directory.get_visibility(db, "!r1:pg.test") == "public"
        assert await directory.published_room_ids(db) == ["!r1:pg.test"]

        await directory.delete_alias(db, alias)
        assert await directory.resolve_alias(db, alias) is None
    finally:
        await db.disconnect()


async def test_concurrent_otk_claims_never_hand_out_same_key() -> None:
    """claim_one_time_key must be atomic across pool connections: at READ COMMITTED
    two concurrent claimers could both SELECT the same row with a select-then-delete,
    handing the same one-time key to two peers. With the single DELETE ... RETURNING
    claim, each uploaded key is handed out exactly once."""
    from neuron_server.storage import e2ee as e2ee_store

    await _reset_schema()
    db = connect_database(_PG, pool_size=8)
    await db.connect()
    try:
        await run_migrations(db)
        user, device = "@alice:pg.test", "DEV"
        n_keys = 5
        await e2ee_store.store_one_time_keys(
            db,
            user,
            device,
            {f"signed_curve25519:K{i}": {"key": f"otk{i}"} for i in range(n_keys)},
        )

        async def claim() -> str | None:
            # Mirrors the /keys/claim service, which wraps each claim in a
            # transaction (its own pool connection here).
            async with db.transaction():
                key = await e2ee_store.claim_one_time_key(db, user, device, "signed_curve25519")
            return next(iter(key)) if key else None

        results = await asyncio.gather(*(claim() for _ in range(n_keys * 2)))
        claimed = [k for k in results if k is not None]
        # No fallback key uploaded, so every non-None result is an OTK: each key is
        # handed out exactly once, and the surplus claimers get nothing.
        assert sorted(claimed) == sorted(f"signed_curve25519:K{i}" for i in range(n_keys))
    finally:
        await db.disconnect()


async def test_edu_queue_on_postgres() -> None:
    """Migration 28 + the durable EDU outbox on real Postgres/asyncpg: an EDU
    round-trips through enqueue/claim/delete, the lease is exclusive, release hands
    it back, destinations_with_pending unions both outboxes, and the inbound
    (origin, message_id) to-device dedup short-circuits a redelivery."""
    from neuron_server.storage import e2ee as e2ee_store
    from neuron_server.storage import outbox as outbox_store

    await _reset_schema()
    db = connect_database(_PG, pool_size=4)
    await db.connect()
    try:
        await run_migrations(db)
        await db.ensure_stream_sequences()  # the edu_outbox stream uses a sequence
        dest = "b.test"

        # Enqueue + exclusive lease + delete round-trip.
        await outbox_store.enqueue_edu(db, dest, {"edu_type": "m.direct_to_device", "n": 1})
        await outbox_store.enqueue_edu(db, dest, {"edu_type": "m.device_list_update", "n": 2})
        claimed = await outbox_store.claim_pending_edus(
            db, dest, "owner-a", now_ms=1000, lease_until_ms=61000
        )
        assert [e["n"] for _, e in claimed] == [1, 2]
        # A concurrent worker sees them leased and claims nothing.
        assert await outbox_store.claim_pending_edus(
            db, dest, "owner-b", now_ms=2000, lease_until_ms=62000
        ) == []

        # Union with the PDU outbox: a PDU-only destination is still offered.
        await outbox_store.enqueue(db, "c.test", {"type": "m.room.message"})
        assert set(await outbox_store.destinations_with_pending(db, 2000)) == {"c.test"}

        # Release hands the EDUs back; delete removes them.
        await outbox_store.release_edus(db, [sid for sid, _ in claimed], "owner-a")
        again = await outbox_store.claim_pending_edus(
            db, dest, "owner-c", now_ms=3000, lease_until_ms=63000
        )
        assert len(again) == 2
        await outbox_store.delete_edus(db, [sid for sid, _ in again], "owner-c")
        assert (
            await db.fetchval(
                "SELECT COUNT(*) FROM federation_edu_outbox WHERE destination = ?", (dest,)
            )
            == 0
        )

        # Inbound to-device dedup: first sighting is new, second short-circuits.
        assert not await e2ee_store.was_to_device_seen(db, "a.test", "msg-1")
        await e2ee_store.mark_to_device_seen(db, "a.test", "msg-1", 1000)
        assert await e2ee_store.was_to_device_seen(db, "a.test", "msg-1")
        # Idempotent re-mark (ON CONFLICT DO NOTHING) does not raise.
        await e2ee_store.mark_to_device_seen(db, "a.test", "msg-1", 2000)
    finally:
        await db.disconnect()


async def test_pushers_and_notifications_on_postgres() -> None:
    """Migration 26 + the pushers/notifications storage on real Postgres/asyncpg:
    pusher upsert is unique per (user, app_id, pushkey), append=false clears the
    pushkey from other users, and a recorded notification lists back (read flag
    computed against a receipt) — the same behaviour as on SQLite."""
    from neuron_server.rooms.events import Event
    from neuron_server.storage import notifications as notif_store
    from neuron_server.storage import pushers as pusher_store
    from neuron_server.storage import receipts as receipts_store
    from neuron_server.storage import rooms as rooms_store

    await _reset_schema()
    db = connect_database(_PG, pool_size=4)
    await db.connect()
    try:
        await run_migrations(db)
        await db.ensure_stream_sequences()
        alice, bob = "@alice:pg.test", "@bob:pg.test"

        # Pusher upsert + uniqueness (a second set for the same key updates in place).
        await pusher_store.upsert_pusher(
            db, alice, app_id="app", pushkey="k1", kind="http",
            app_display_name="A", device_display_name="Phone", profile_tag=None,
            lang="en", data={"url": "https://gw/notify"}, ts=1000,
        )
        await pusher_store.upsert_pusher(
            db, bob, app_id="app", pushkey="k1", kind="http",
            app_display_name="B", device_display_name="Phone", profile_tag=None,
            lang="en", data={"url": "https://gw/notify"}, ts=1000,
        )
        assert len(await pusher_store.get_pushers(db, alice)) == 1
        # append=false semantics: bob claiming k1 removes it from alice.
        await pusher_store.delete_pushkey_elsewhere(db, bob, "app", "k1")
        assert await pusher_store.get_pushers(db, alice) == []
        assert len(await pusher_store.get_pushers(db, bob)) == 1

        # Seed a room + an event so notifications/receipts can join to it.
        room_id = "!r:pg.test"
        await rooms_store.create_room_row(db, room_id, alice, "11", 1000)
        stream = await db.next_stream_id("events")
        event = Event(
            event_id="$e1", room_id=room_id, type="m.room.message", sender=alice,
            content={"body": "hi bob"}, origin_server_ts=1000, depth=1,
            stream_ordering=stream,
        )
        await rooms_store.insert_event(db, event)

        await notif_store.record(
            db, bob, event_id="$e1", room_id=room_id,
            actions=["notify", {"set_tweak": "highlight"}], ts=2000, highlight=True,
        )
        entries, next_from = await notif_store.list_for_user(
            db, bob, limit=10, from_ts=None, only_highlight=False
        )
        assert next_from is None
        assert len(entries) == 1
        notification, read = entries[0]
        assert notification.event.event_id == "$e1" and read is False

        # only=highlight keeps it; a receipt at the event flips read True.
        hl, _ = await notif_store.list_for_user(
            db, bob, limit=10, from_ts=None, only_highlight=True
        )
        assert len(hl) == 1
        await receipts_store.upsert_receipt(db, room_id, bob, "m.read", "$e1", 3000)
        entries, _ = await notif_store.list_for_user(
            db, bob, limit=10, from_ts=None, only_highlight=False
        )
        assert entries[0][1] is True
    finally:
        await db.disconnect()
