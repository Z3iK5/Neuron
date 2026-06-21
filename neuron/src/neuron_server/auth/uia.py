# SPDX-License-Identifier: Apache-2.0
"""User-Interactive Authentication (UIA) session tracking.

The Client-Server API authenticates some requests (e.g. registration) through a
multi-stage flow: the server replies ``401`` with a list of acceptable flows and
a ``session`` id; the client repeats the request, supplying ``auth`` that
references that session. We track the open sessions here.

The only flow we need today is ``m.login.dummy`` (registration). Sessions live in
the database so the challenge and the retry can be served by different workers
(no sticky load balancer required). A background sweep removes sessions older than
the configured TTL, since an abandoned challenge would otherwise leave a row
behind forever.
"""

from __future__ import annotations

import secrets
import time

from neuron_server.storage import uia as uia_store
from neuron_server.storage.database import Database


def _now_ms() -> int:
    return int(time.time() * 1000)


class UiaSessionStore:
    """A database-backed set of open UIA session ids (shared across workers)."""

    def __init__(self, db: Database, *, ttl_ms: int) -> None:
        self._db = db
        self._ttl_ms = ttl_ms

    async def create(self) -> str:
        """Open a new session and return its id."""
        session_id = secrets.token_urlsafe(16)
        await uia_store.create_session(self._db, session_id, _now_ms())
        return session_id

    async def exists(self, session_id: str) -> bool:
        """Return True if ``session_id`` is a known, unexpired open session."""
        return await uia_store.session_exists(
            self._db, session_id, min_created_ts=_now_ms() - self._ttl_ms
        )

    async def complete(self, session_id: str) -> None:
        """Close a session once its flow has been satisfied."""
        await uia_store.delete_session(self._db, session_id)

    async def sweep_expired(self) -> None:
        """Delete sessions older than the TTL (called periodically by a sweeper)."""
        await uia_store.delete_expired(self._db, _now_ms() - self._ttl_ms)
