# SPDX-License-Identifier: Apache-2.0
"""Tests for the /sync notifier seam, the backend factory, and the broadcast wrap.

The cross-process Postgres transport is exercised end-to-end in
``tests/integration/test_postgres.py``; here we cover the in-process behaviour and
the fan-out logic with a fake transport (no database needed).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from neuron_server.config import NeuronServerSettings
from neuron_server.storage.database import connect_database
from neuron_server.sync.broadcast import BroadcastNotifier
from neuron_server.sync.notifier import Notifier, StreamNotifier, build_notifier

_SQLITE = "sqlite:///:memory:"
_PG = "postgresql://u:p@localhost/db"


def _settings(url: str, backend: str = "auto") -> NeuronServerSettings:
    return NeuronServerSettings(name="neuron.local", database_url=url, notifier_backend=backend)


def test_build_notifier_sqlite_is_in_process() -> None:
    db = connect_database(_SQLITE)
    notifier = build_notifier(_settings(_SQLITE), db)
    assert isinstance(notifier, StreamNotifier)
    assert isinstance(notifier, Notifier)  # runtime_checkable protocol conformance


def test_build_notifier_postgres_is_broadcast() -> None:
    db = connect_database(_PG)
    notifier = build_notifier(_settings(_PG), db)
    assert isinstance(notifier, BroadcastNotifier)
    assert isinstance(notifier, Notifier)


def test_build_notifier_inprocess_override_on_postgres() -> None:
    db = connect_database(_PG)
    notifier = build_notifier(_settings(_PG, backend="inprocess"), db)
    assert isinstance(notifier, StreamNotifier)


def test_build_notifier_pg_backend_requires_postgres() -> None:
    db = connect_database(_SQLITE)
    with pytest.raises(ValueError, match="postgresql"):
        build_notifier(_settings(_SQLITE, backend="pg"), db)


def test_build_notifier_rejects_unknown_and_redis() -> None:
    db = connect_database(_SQLITE)
    with pytest.raises(ValueError, match="redis"):
        build_notifier(_settings(_SQLITE, backend="redis"), db)
    with pytest.raises(ValueError, match="unknown notifier_backend"):
        build_notifier(_settings(_SQLITE, backend="bogus"), db)


async def test_stream_notifier_wakes_waiter() -> None:
    notifier = StreamNotifier()
    waiter = asyncio.create_task(notifier.wait(5.0))
    await asyncio.sleep(0.02)
    notifier.notify()
    await asyncio.wait_for(waiter, timeout=1.0)  # would raise if not woken


async def test_stream_notifier_times_out_without_notify() -> None:
    notifier = StreamNotifier()
    # Returns (does not raise) on timeout — swallows TimeoutError by contract.
    await asyncio.wait_for(notifier.wait(0.05), timeout=1.0)


class _FakeTransport:
    """Records publishes and captures the on_ping callback start() is given."""

    def __init__(self) -> None:
        self.published = 0
        self.started = False
        self.stopped = False
        self.on_ping: Callable[[], None] | None = None

    async def start(self, on_ping: Callable[[], None]) -> None:
        self.started = True
        self.on_ping = on_ping

    async def publish(self) -> None:
        self.published += 1

    async def stop(self) -> None:
        self.stopped = True


async def test_broadcast_notify_wakes_local_and_publishes() -> None:
    local = StreamNotifier()
    transport = _FakeTransport()
    notifier = BroadcastNotifier(local, transport)
    await notifier.start()
    assert transport.started and transport.on_ping is not None

    waiter = asyncio.create_task(notifier.wait(5.0))
    await asyncio.sleep(0.02)
    notifier.notify()
    await asyncio.wait_for(waiter, timeout=1.0)  # local waiter woken synchronously

    await asyncio.sleep(0)  # let the fire-and-forget publish task run
    assert transport.published == 1

    await notifier.stop()
    assert transport.stopped


async def test_broadcast_incoming_ping_wakes_local_waiter() -> None:
    local = StreamNotifier()
    transport = _FakeTransport()
    notifier = BroadcastNotifier(local, transport)
    await notifier.start()

    waiter = asyncio.create_task(notifier.wait(5.0))
    await asyncio.sleep(0.02)
    # Simulate a ping arriving from another worker: the transport calls on_ping.
    assert transport.on_ping is not None
    transport.on_ping()
    await asyncio.wait_for(waiter, timeout=1.0)
    # A received ping must NOT re-publish (no echo storm).
    assert transport.published == 0
