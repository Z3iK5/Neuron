"""The Auditor: a /sync loop that records room events to a sink.

Flow of one poll:

1. Ask Synapse for everything since our saved token (``GET /sync?since=…``).
2. Optionally auto-join rooms we've been invited to.
3. Write every timeline event from joined rooms to the sink as an audit record.
4. Save the new token so a restart resumes with no gaps and no duplicates.

The token persistence (see ``state.py``) is what makes restarts safe: Synapse
only re-sends events *after* the token.
"""

from __future__ import annotations

import asyncio

from neuron_auditor.sinks import Sink, build_audit_record
from neuron_auditor.state import StateStore
from neuron_core import MatrixClient, get_logger
from neuron_core.errors import MatrixError

log = get_logger(__name__)


class Auditor:
    """Streams room events from Synapse into a durable sink."""

    def __init__(
        self,
        client: MatrixClient,
        sink: Sink,
        state: StateStore,
        *,
        auto_join: bool = True,
        sync_timeout_ms: int = 30000,
        retry_seconds: float = 5.0,
    ) -> None:
        self.client = client
        self.sink = sink
        self.state = state
        self.auto_join = auto_join
        self.sync_timeout_ms = sync_timeout_ms
        self.retry_seconds = retry_seconds

    async def poll_once(self) -> int:
        """Run one sync + record cycle. Returns the number of events written."""
        since = self.state.get_since()
        response = await self.client.sync(since=since, timeout_ms=self.sync_timeout_ms)
        rooms = response.get("rooms", {})

        if self.auto_join:
            await self._accept_invites(rooms.get("invite", {}))

        written = self._record_joined(rooms.get("join", {}))

        next_batch = response.get("next_batch")
        if next_batch:
            self.state.set_since(next_batch)
        return written

    async def run_forever(self) -> None:
        """Poll in a loop. Transient errors are logged and retried, not fatal."""
        log.info("auditor starting", extra={"auto_join": self.auto_join})
        while True:
            try:
                count = await self.poll_once()
                if count:
                    log.info("recorded events", extra={"count": count})
            except Exception:  # keep the daemon alive across transient failures
                log.exception("poll failed; retrying")
                await asyncio.sleep(self.retry_seconds)

    async def _accept_invites(self, invites: dict[str, object]) -> None:
        for room_id in invites:
            try:
                await self.client.join_room(room_id)
                log.info("joined invited room", extra={"room_id": room_id})
            except MatrixError:
                log.warning("could not join invited room", extra={"room_id": room_id})

    def _record_joined(self, joined: dict[str, dict[str, object]]) -> int:
        count = 0
        for room_id, room_data in joined.items():
            timeline = room_data.get("timeline", {})
            events = timeline.get("events", []) if isinstance(timeline, dict) else []
            for event in events:
                self.sink.write(build_audit_record(room_id, event))
                count += 1
        return count
