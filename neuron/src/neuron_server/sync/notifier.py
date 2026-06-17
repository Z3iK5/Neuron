# SPDX-License-Identifier: Apache-2.0
"""A tiny wake-up primitive for long-polling ``/sync``.

When an event is appended, :meth:`StreamNotifier.notify` wakes every sync that is
currently waiting. A waiter blocks (up to a timeout) until woken, then re-checks
the database. This avoids busy-polling while keeping the implementation small;
it wakes all waiters on any change, which is fine for the single-server MVP.
"""

from __future__ import annotations

import asyncio


class StreamNotifier:
    """Wakes waiting ``/sync`` requests when new events arrive."""

    def __init__(self) -> None:
        self._waiters: list[asyncio.Future[None]] = []

    def notify(self) -> None:
        """Wake all currently-waiting syncs."""
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters = []

    async def wait(self, timeout_seconds: float) -> None:
        """Block until the next :meth:`notify` or until ``timeout_seconds`` elapse."""
        loop = asyncio.get_event_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout_seconds)
        except TimeoutError:
            pass
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
