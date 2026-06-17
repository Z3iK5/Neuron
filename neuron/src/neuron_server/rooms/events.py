# SPDX-License-Identifier: Apache-2.0
"""The in-memory event model and ID/room-ID generation.

An :class:`Event` is the server's representation of a Matrix event. ``state_key``
is ``None`` for non-state events. ``stream_ordering`` is a server-local monotonic
position used for ``/sync`` and ``/messages`` pagination; ``depth`` is the event's
position in the room DAG.

Event IDs are opaque, server-generated strings (``$<random>``). Federation-grade
**reference-hash** event IDs (and content hashing / signing) are deferred to the
federation epic (HS-7); clients treat the event ID as opaque, so this is a safe
simplification for the single-server MVP.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any


def generate_room_id(server_name: str) -> str:
    """Return a fresh room ID (``!<random>:server_name``)."""
    return f"!{secrets.token_urlsafe(16)}:{server_name}"


def generate_event_id() -> str:
    """Return a fresh opaque event ID (``$<random>``)."""
    return f"${secrets.token_urlsafe(32)}"


@dataclass
class Event:
    """A stored Matrix event."""

    event_id: str
    room_id: str
    type: str
    sender: str
    content: dict[str, Any]
    origin_server_ts: int
    depth: int
    stream_ordering: int
    state_key: str | None = None
    unsigned: dict[str, Any] | None = None
    redacts: str | None = None

    @property
    def is_state(self) -> bool:
        return self.state_key is not None

    def client_dict(self) -> dict[str, Any]:
        """Render the event in the Client-Server API shape."""
        body: dict[str, Any] = {
            "event_id": self.event_id,
            "type": self.type,
            "sender": self.sender,
            "content": self.content,
            "origin_server_ts": self.origin_server_ts,
            "room_id": self.room_id,
        }
        if self.state_key is not None:
            body["state_key"] = self.state_key
        if self.redacts is not None:
            body["redacts"] = self.redacts
        if self.unsigned:
            body["unsigned"] = self.unsigned
        return body
