# SPDX-License-Identifier: Apache-2.0
"""The in-memory event model and room-ID generation.

An :class:`Event` is the server's representation of a Matrix event. ``state_key``
is ``None`` for non-state events. ``stream_ordering`` is a server-local monotonic
position used for ``/sync`` and ``/messages`` pagination; ``depth`` is the event's
position in the room DAG.

Event IDs are **reference hashes** (``$`` + URL-safe base64 of the event's
SHA-256 reference hash, per room version 11), computed when the event is built;
``pdu`` holds the full signed federation event (``auth_events``/``prev_events``/
``hashes``/``signatures``) so it can be served and verified over federation.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any


def generate_room_id(server_name: str) -> str:
    """Return a fresh room ID (``!<random>:server_name``)."""
    return f"!{secrets.token_urlsafe(16)}:{server_name}"


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
    auth_events: list[str] = field(default_factory=list)
    prev_events: list[str] = field(default_factory=list)
    hashes: dict[str, Any] | None = None
    signatures: dict[str, Any] | None = None

    @property
    def is_state(self) -> bool:
        return self.state_key is not None

    @classmethod
    def from_pdu(cls, pdu: dict[str, Any], event_id: str, stream_ordering: int) -> Event:
        """Build a stored event from a received federation PDU."""
        state_key = pdu.get("state_key")
        return cls(
            event_id=event_id,
            room_id=str(pdu["room_id"]),
            type=str(pdu["type"]),
            sender=str(pdu["sender"]),
            content=dict(pdu.get("content") or {}),
            origin_server_ts=int(pdu["origin_server_ts"]),
            depth=int(pdu.get("depth", 0)),
            stream_ordering=stream_ordering,
            state_key=None if state_key is None else str(state_key),
            auth_events=list(pdu.get("auth_events", [])),
            prev_events=list(pdu.get("prev_events", [])),
            hashes=pdu.get("hashes"),
            signatures=pdu.get("signatures"),
        )

    def pdu_dict(self) -> dict[str, Any]:
        """Render the full federation event (PDU) shape.

        Room v3+ events carry no ``event_id`` field — the ID is the reference hash
        of this object — so it is deliberately omitted here.
        """
        body: dict[str, Any] = {
            "room_id": self.room_id,
            "type": self.type,
            "sender": self.sender,
            "content": self.content,
            "origin_server_ts": self.origin_server_ts,
            "depth": self.depth,
            "auth_events": self.auth_events,
            "prev_events": self.prev_events,
        }
        if self.state_key is not None:
            body["state_key"] = self.state_key
        if self.hashes is not None:
            body["hashes"] = self.hashes
        if self.signatures is not None:
            body["signatures"] = self.signatures
        return body

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
