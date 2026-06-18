# SPDX-License-Identifier: Apache-2.0
"""The background federation retry flusher (HS-7 step 6l)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from neuron_server.config import NeuronServerSettings
from neuron_server.federation.flusher import RetryFlusher
from neuron_server.federation.sender import FederationSender
from neuron_server.storage import outbox as outbox_store
from neuron_server.storage.database import connect_database
from neuron_server.storage.migrations import run_migrations


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_flusher_runs_periodically_and_stops_cleanly() -> None:
    calls = 0

    async def flush() -> None:
        nonlocal calls
        calls += 1

    flusher = RetryFlusher(flush, interval_s=0.01)
    flusher.start()
    assert await _wait_until(lambda: calls >= 2)
    await flusher.stop()

    settled = calls
    await asyncio.sleep(0.05)
    assert calls == settled  # no further flushes after stop


async def test_flusher_survives_flush_errors() -> None:
    calls = 0

    async def boom() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("transient")

    flusher = RetryFlusher(boom, interval_s=0.01)
    flusher.start()
    try:
        # The loop keeps going despite each flush raising.
        assert await _wait_until(lambda: calls >= 3)
    finally:
        await flusher.stop()


class _RecordingClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def put_json(self, destination: str, path: str, body: dict) -> dict:
        self.sent.append(destination)
        return {}


async def test_retry_all_drains_every_destination(tmp_path: Path) -> None:
    db = connect_database(f"sqlite:///{tmp_path / 'hs.db'}")
    await db.connect()
    try:
        await run_migrations(db)
        await outbox_store.enqueue(db, "b.test", {"type": "m.room.message"})
        await outbox_store.enqueue(db, "c.test", {"type": "m.room.message"})

        client = _RecordingClient()
        sender = FederationSender(db, "a.test", client)  # type: ignore[arg-type]
        await sender.retry_all()

        assert set(client.sent) == {"b.test", "c.test"}
        assert not await outbox_store.get_pending(db, "b.test")
        assert not await outbox_store.get_pending(db, "c.test")
    finally:
        await db.disconnect()


def test_settings_expose_retry_interval() -> None:
    assert NeuronServerSettings(name="hs").federation_retry_interval_s > 0
