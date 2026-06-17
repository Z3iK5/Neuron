# SPDX-License-Identifier: Apache-2.0
"""User-Interactive Authentication (UIA) session tracking.

The Client-Server API authenticates some requests (e.g. registration) through a
multi-stage flow: the server replies ``401`` with a list of acceptable flows and
a ``session`` id; the client repeats the request, supplying ``auth`` that
references that session. We track the open sessions here.

For HS-1 the only flow we need is ``m.login.dummy`` (registration). Sessions are
held in memory — they are short-lived and it is fine for them not to survive a
restart (the client simply starts the flow again).
"""

from __future__ import annotations

import secrets


class UiaSessionStore:
    """An in-memory set of open UIA session ids."""

    def __init__(self) -> None:
        self._sessions: set[str] = set()

    def create(self) -> str:
        """Open a new session and return its id."""
        session_id = secrets.token_urlsafe(16)
        self._sessions.add(session_id)
        return session_id

    def exists(self, session_id: str) -> bool:
        """Return True if ``session_id`` is a known open session."""
        return session_id in self._sessions

    def complete(self, session_id: str) -> None:
        """Close a session once its flow has been satisfied."""
        self._sessions.discard(session_id)
