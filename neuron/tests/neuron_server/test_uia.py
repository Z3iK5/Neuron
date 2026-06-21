# SPDX-License-Identifier: Apache-2.0
"""Shared (database-backed) User-Interactive-Auth sessions.

The point of moving UIA sessions into the database is that the 401 challenge and
the client's retry can be served by *different* workers. These tests model that by
using two independent AuthService instances over one shared database.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest_asyncio

from neuron_server.auth.service import AuthService
from neuron_server.storage import uia as uia_store
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.migrations import run_migrations


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = connect_database("sqlite:///:memory:")
    await database.connect()
    await run_migrations(database)
    try:
        yield database
    finally:
        await database.disconnect()


def _auth(db: Database, *, ttl_s: float = 3600.0) -> AuthService:
    return AuthService(db, "neuron.local", True, uia_session_ttl_s=ttl_s)


def _dummy(session: str) -> dict[str, str]:
    return {"type": "m.login.dummy", "session": session}


async def test_session_created_on_one_worker_is_valid_on_another(db: Database) -> None:
    worker_a = _auth(db)
    worker_b = _auth(db)

    session = await worker_a.begin_uia()
    # The retry lands on a different worker — it must still recognise the session.
    assert await worker_b.uia_satisfied(_dummy(session)) is True


async def test_complete_closes_the_session_everywhere(db: Database) -> None:
    worker_a = _auth(db)
    worker_b = _auth(db)

    session = await worker_a.begin_uia()
    await worker_b.complete_uia(_dummy(session))
    # Once completed, neither worker accepts the session again (no replay).
    assert await worker_a.uia_satisfied(_dummy(session)) is False
    assert await worker_b.uia_satisfied(_dummy(session)) is False


async def test_unknown_or_malformed_auth_is_not_satisfied(db: Database) -> None:
    auth = _auth(db)
    assert await auth.uia_satisfied(None) is False
    assert await auth.uia_satisfied({"type": "m.login.dummy", "session": "nope"}) is False
    assert await auth.uia_satisfied({"type": "m.login.password", "session": "x"}) is False
    # A real session but wrong stage type is rejected.
    session = await auth.begin_uia()
    assert await auth.uia_satisfied({"type": "m.login.password", "session": session}) is False


async def test_sweep_removes_expired_but_keeps_fresh(db: Database) -> None:
    auth = _auth(db, ttl_s=3600.0)
    fresh = await auth.begin_uia()
    # An old session inserted directly with a long-past timestamp.
    now_ms = int(time.time() * 1000)
    await uia_store.create_session(db, "stale-session", now_ms - 2 * 3600 * 1000)

    await auth.sweep_uia()

    assert await uia_store.session_exists(db, "stale-session") is False
    assert await uia_store.session_exists(db, fresh) is True


async def test_uia_satisfied_rejects_expired_session_before_sweep(db: Database) -> None:
    # The read path must enforce the TTL itself, not only the background sweeper, so
    # an expired-but-not-yet-swept session is rejected.
    auth = _auth(db, ttl_s=3600.0)
    now_ms = int(time.time() * 1000)
    await uia_store.create_session(db, "stale", now_ms - 2 * 3600 * 1000)
    assert await auth.uia_satisfied(_dummy("stale")) is False


async def test_session_exists_enforces_created_ts_cutoff(db: Database) -> None:
    await uia_store.create_session(db, "old", 1000)
    assert await uia_store.session_exists(db, "old", min_created_ts=5000) is False
    assert await uia_store.session_exists(db, "old", min_created_ts=0) is True


async def test_storage_delete_expired_uses_cutoff(db: Database) -> None:
    await uia_store.create_session(db, "old", 1000)
    await uia_store.create_session(db, "new", 10_000)
    await uia_store.delete_expired(db, cutoff_ts=5000)
    assert await uia_store.session_exists(db, "old") is False
    assert await uia_store.session_exists(db, "new") is True
