# SPDX-License-Identifier: Apache-2.0
"""Background flusher for the federation send outbox (HS-7 step 6l).

Periodically retries delivery of any queued events, so a destination that was
offline gets its backlog without waiting for the next locally-originated event.
The flush callable is injected (the app passes ``FederationSender.retry_all``) so
the loop is testable without a real sender.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from neuron_core import get_logger

_logger = get_logger(__name__)


class RetryFlusher:
    """Runs an async callable on a fixed interval until stopped.

    A small generic interval runner — used for the federation send-outbox retry and
    the multi-writer stream-position heartbeat. ``name`` only labels error logs.
    """

    def __init__(
        self,
        flush: Callable[[], Awaitable[None]],
        interval_s: float = 30.0,
        *,
        name: str = "federation retry flush",
    ) -> None:
        self._flush = flush
        self._interval = interval_s
        self._name = name
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._flush()
            except Exception:  # a failed run must not kill the loop
                _logger.exception("%s failed", self._name)
            await asyncio.sleep(self._interval)
