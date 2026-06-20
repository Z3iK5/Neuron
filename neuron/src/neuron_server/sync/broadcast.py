# SPDX-License-Identifier: Apache-2.0
"""A notifier that broadcasts wakes across worker processes.

:class:`BroadcastNotifier` wraps a local :class:`~neuron_server.sync.notifier.
StreamNotifier` with a :class:`~neuron_server.sync.transport.NotifierTransport`.
``notify()`` wakes same-process waiters instantly *and* publishes a cross-process
ping; the transport's incoming pings are routed back into the local notifier, so
a ``/sync`` parked on any worker is woken by a change on any other worker.

``notify()`` stays a zero-arg synchronous callable (the load-bearing seam), so the
async publish is scheduled fire-and-forget on the running loop.
"""

from __future__ import annotations

import asyncio

from neuron_core import get_logger
from neuron_server.sync.notifier import StreamNotifier
from neuron_server.sync.transport import NotifierTransport

_logger = get_logger(__name__)


class BroadcastNotifier:
    """Local wake + cross-process wake over a :class:`NotifierTransport`."""

    def __init__(self, local: StreamNotifier, transport: NotifierTransport) -> None:
        self._local = local
        self._transport = transport
        # Hold references to in-flight publish tasks so they aren't GC'd mid-flight.
        self._pending: set[asyncio.Task[None]] = set()

    def notify(self) -> None:
        # Wake this process's waiters synchronously (no latency for the common
        # same-worker case), then fan the ping out to other workers.
        self._local.notify()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - notify is always called in a loop
            return
        task = loop.create_task(self._publish())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _publish(self) -> None:
        try:
            await self._transport.publish()
        except Exception:  # a failed ping degrades to other workers' timeout fallback
            _logger.exception("notifier broadcast publish failed")

    async def wait(self, timeout_seconds: float) -> None:
        await self._local.wait(timeout_seconds)

    async def start(self) -> None:
        # Incoming cross-process pings wake this worker's local waiters.
        await self._transport.start(self._local.notify)

    async def stop(self) -> None:
        await self._transport.stop()
        # Cancel any in-flight publishes so they don't run pg_notify against a pool
        # that lifespan is about to disconnect (publishes are best-effort pings;
        # dropping them on shutdown is harmless).
        pending = list(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
