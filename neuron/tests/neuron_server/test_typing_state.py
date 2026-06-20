# SPDX-License-Identifier: Apache-2.0
"""Tests for the DB-backed TypingHandler (cross-process typing state).

Run against SQLite here; the Postgres path (and cross-worker visibility) is
covered in ``tests/integration/test_postgres.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.migrations import run_migrations
from neuron_server.typing_state import TypingHandler

_ROOM = "!room:neuron.local"
_ALICE = "@alice:neuron.local"
_BOB = "@bob:neuron.local"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = connect_database("sqlite:///:memory:")
    await database.connect()
    await run_migrations(database)
    await database.ensure_stream_sequences()
    try:
        yield database
    finally:
        await database.disconnect()


async def test_start_typing_is_visible_and_bumps_serial(db: Database) -> None:
    handler = TypingHandler(db)
    assert await handler.serial() == 0
    await handler.set_typing(_ROOM, _ALICE, True)
    assert await handler.typing_users(_ROOM) == [_ALICE]
    assert await handler.serial() == 1


async def test_users_sorted_and_scoped_per_room(db: Database) -> None:
    handler = TypingHandler(db)
    await handler.set_typing(_ROOM, _BOB, True)
    await handler.set_typing(_ROOM, _ALICE, True)
    assert await handler.typing_users(_ROOM) == [_ALICE, _BOB]  # sorted
    assert await handler.typing_users("!other:neuron.local") == []


async def test_stop_typing_removes_user_and_bumps_serial(db: Database) -> None:
    handler = TypingHandler(db)
    await handler.set_typing(_ROOM, _ALICE, True)
    serial_after_start = await handler.serial()
    await handler.set_typing(_ROOM, _ALICE, False)
    assert await handler.typing_users(_ROOM) == []
    # The serial advances on stop (so /sync wakes) and never regresses.
    assert await handler.serial() > serial_after_start


async def test_stop_when_not_typing_is_a_noop(db: Database) -> None:
    notified: list[int] = []
    handler = TypingHandler(db, notify=lambda: notified.append(1))
    before = await handler.serial()
    await handler.set_typing(_ROOM, _ALICE, False)  # was never typing
    assert await handler.serial() == before
    assert notified == []  # no spurious wake


async def test_notify_fires_on_change_only(db: Database) -> None:
    notified: list[int] = []
    handler = TypingHandler(db, notify=lambda: notified.append(1))
    await handler.set_typing(_ROOM, _ALICE, True)  # change -> notify
    await handler.set_typing(_ROOM, _ALICE, False)  # change -> notify
    assert len(notified) == 2


async def test_stream_id_never_regresses_on_lower_concurrent_write(db: Database) -> None:
    """Under concurrent Postgres, sequence allocation order can differ from commit
    order, so a later UPSERT may carry a *lower* id. The CASE-max guard must keep
    the row (and thus the serial) from moving backwards. Forced here with a direct
    write of a lower id, since a single SQLite connection can't reorder."""
    handler = TypingHandler(db)
    await handler.set_typing(_ROOM, _ALICE, True)
    await db.execute(
        "UPDATE typing SET stream_id = ? WHERE room_id = ? AND user_id = ?",
        (100, _ROOM, _ALICE),
    )
    assert await handler.serial() == 100
    # Mimic an out-of-order commit carrying a lower id (the production UPSERT shape).
    await db.execute(
        "INSERT INTO typing (room_id, user_id, expiry_ms, stream_id) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(room_id, user_id) DO UPDATE SET expiry_ms = excluded.expiry_ms,"
        " stream_id = CASE WHEN excluded.stream_id > typing.stream_id"
        " THEN excluded.stream_id ELSE typing.stream_id END",
        (_ROOM, _ALICE, 999, 50),
    )
    assert await handler.serial() == 100  # did not regress to 50


async def test_expired_typing_excluded_but_serial_monotonic(db: Database) -> None:
    handler = TypingHandler(db)
    # A zero/negative timeout expires immediately.
    await handler.set_typing(_ROOM, _ALICE, True, timeout_ms=0)
    serial_after = await handler.serial()
    assert await handler.typing_users(_ROOM) == []  # already expired
    # The allocated stream id stands — the serial does not regress on expiry.
    assert serial_after >= 1
