# SPDX-License-Identifier: Apache-2.0
"""In-memory typing notifications (HS-7 step 6k).

Typing is ephemeral and time-limited, so it lives in memory rather than the
database. A monotonic ``serial`` is bumped on every change so ``/sync`` can tell
when typing changed (without it, long-polling would never settle); expired entries
are pruned lazily on read.

Single-process only — a multi-worker deployment would need shared state.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# Default lifetime for a typing notification received over federation (the EDU
# carries no timeout of its own).
_DEFAULT_TIMEOUT_MS = 30_000


def _now_ms() -> int:
    return int(time.time() * 1000)


class TypingHandler:
    """Tracks which users are currently typing in each room."""

    def __init__(self, notify: Callable[[], None] | None = None) -> None:
        self._typing: dict[str, dict[str, int]] = {}  # room_id -> {user_id: expiry_ms}
        self._serial = 0
        self._notify = notify

    @property
    def serial(self) -> int:
        return self._serial

    def set_typing(
        self, room_id: str, user_id: str, typing: bool, timeout_ms: int = _DEFAULT_TIMEOUT_MS
    ) -> None:
        users = self._typing.setdefault(room_id, {})
        if typing:
            users[user_id] = _now_ms() + max(0, timeout_ms)
            changed = True
        else:
            changed = users.pop(user_id, None) is not None
        if changed:
            self._serial += 1
            if self._notify is not None:
                self._notify()

    def typing_users(self, room_id: str) -> list[str]:
        """The currently-typing users in a room (expired entries pruned)."""
        users = self._typing.get(room_id)
        if not users:
            return []
        now = _now_ms()
        expired = [user_id for user_id, expiry in users.items() if expiry <= now]
        for user_id in expired:
            del users[user_id]
        return sorted(users)
