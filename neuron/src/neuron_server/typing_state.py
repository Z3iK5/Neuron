# SPDX-License-Identifier: Apache-2.0
"""Typing notifications (HS-7 step 6k), backed by shared storage.

Typing is ephemeral and time-limited, but it must be visible across workers, so
state lives in the database rather than process memory (see
:mod:`neuron_server.storage.typing`). A monotonic ``serial`` (the max typing
stream id) lets ``/sync`` tell when typing changed; expired entries are filtered
on read. The handler keeps its small method surface — ``set_typing``,
``typing_users``, ``serial`` — so ``/sync`` and the federation/client call sites
are unchanged apart from awaiting them.
"""

from __future__ import annotations

from collections.abc import Callable

from neuron_server.clock import now_ms
from neuron_server.storage import typing as typing_store
from neuron_server.storage.database import Database

# Default lifetime for a typing notification received over federation (the EDU
# carries no timeout of its own).
_DEFAULT_TIMEOUT_MS = 30_000



class TypingHandler:
    """Tracks which users are currently typing in each room (DB-backed)."""

    def __init__(self, db: Database, notify: Callable[[], None] | None = None) -> None:
        self._db = db
        self._notify = notify

    async def serial(self) -> int:
        """The monotonic typing stream position (for ``/sync`` change detection)."""
        return await typing_store.max_typing_stream(self._db)

    async def set_typing(
        self, room_id: str, user_id: str, typing: bool, timeout_ms: int = _DEFAULT_TIMEOUT_MS
    ) -> None:
        if typing:
            await typing_store.set_typing(
                self._db, room_id, user_id, now_ms() + max(0, timeout_ms)
            )
            changed = True
        else:
            # Only a transition (was typing -> not) is a change worth waking on,
            # matching the previous in-memory semantics.
            if await typing_store.is_typing(self._db, room_id, user_id, now_ms()):
                await typing_store.set_typing(self._db, room_id, user_id, 0)
                changed = True
            else:
                changed = False
        if changed and self._notify is not None:
            self._notify()

    async def typing_users(self, room_id: str) -> list[str]:
        """The currently-typing users in a room (expired entries excluded)."""
        return await typing_store.typing_users(self._db, room_id, now_ms())
