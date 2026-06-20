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
