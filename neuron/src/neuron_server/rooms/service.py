# SPDX-License-Identifier: Apache-2.0
"""Room domain service: create rooms, send events, moderate, read state/history.

Single-server model: events form a linear DAG, so the room's *current state* is
the authorization context for each new event (no state resolution — that is part
of the federation epic, HS-7). Every client-originated event is checked against
:mod:`neuron_server.rooms.authrules` before it is persisted.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from neuron_server.errors import MatrixError
from neuron_server.rooms import authrules, versions
from neuron_server.rooms.authrules import AuthState
from neuron_server.rooms.events import Event, generate_event_id, generate_room_id
from neuron_server.storage import rooms as store
from neuron_server.storage.database import Database

_DEFAULT_HISTORY_VISIBILITY = "shared"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_power_levels(creator: str) -> dict[str, Any]:
    """The power-level defaults a freshly created room starts with."""
    return {
        "users": {creator: 100},
        "users_default": 0,
        "events": {
            "m.room.name": 50,
            "m.room.power_levels": 100,
            "m.room.history_visibility": 100,
            "m.room.canonical_alias": 50,
            "m.room.avatar": 50,
            "m.room.topic": 50,
            "m.room.encryption": 100,
            "m.room.server_acl": 100,
        },
        "events_default": 0,
        "state_default": 50,
        "ban": 50,
        "kick": 50,
        "redact": 50,
        "invite": 0,
        "notifications": {"room": 50},
    }


class RoomService:
    """Create and operate on rooms for one server."""

    def __init__(
        self, db: Database, server_name: str, notify: Callable[[], None] | None = None
    ) -> None:
        self._db = db
        self._server_name = server_name
        self._notify = notify

    def _wake_syncs(self) -> None:
        if self._notify is not None:
            self._notify()

    # --- internals ---------------------------------------------------------

    async def _load_state(self, room_id: str) -> AuthState:
        events = await store.get_current_state(self._db, room_id)
        return {(e.type, e.state_key or ""): e for e in events}

    async def _append(
        self,
        room_id: str,
        *,
        etype: str,
        sender: str,
        content: dict[str, Any],
        state_key: str | None = None,
        ts: int | None = None,
        unsigned: dict[str, Any] | None = None,
        redacts: str | None = None,
    ) -> Event:
        """Persist a new event, updating current state and membership. No auth check."""
        stream = await store.next_stream_ordering(self._db)
        depth = await store.next_depth(self._db, room_id)
        event = Event(
            event_id=generate_event_id(),
            room_id=room_id,
            type=etype,
            sender=sender,
            content=content,
            origin_server_ts=ts if ts is not None else _now_ms(),
            depth=depth,
            stream_ordering=stream,
            state_key=state_key,
            unsigned=unsigned,
            redacts=redacts,
        )
        await store.insert_event(self._db, event)
        if state_key is not None:
            await store.update_current_state(self._db, room_id, etype, state_key, event.event_id)
        if etype == "m.room.member":
            await store.set_membership(
                self._db, room_id, state_key or "", str(content.get("membership"))
            )
        return event

    async def _require_room(self, room_id: str) -> store.RoomRow:
        room = await store.get_room(self._db, room_id)
        if room is None:
            raise MatrixError(404, "M_NOT_FOUND", "Unknown room")
        return room

    # --- create ------------------------------------------------------------

    async def create_room(self, creator: str, body: dict[str, Any]) -> str:
        """Create a room and return its room ID."""
        room_version = body.get("room_version") or versions.DEFAULT_ROOM_VERSION
        if not versions.is_supported(room_version):
            raise MatrixError(
                400, "M_UNSUPPORTED_ROOM_VERSION", f"Unsupported room version {room_version!r}"
            )

        visibility = body.get("visibility", "private")
        preset = body.get("preset") or (
            "public_chat" if visibility == "public" else "private_chat"
        )
        join_rule = "public" if preset == "public_chat" else "invite"

        room_id = generate_room_id(self._server_name)
        ts = _now_ms()

        async with self._db.transaction():
            await store.create_room_row(self._db, room_id, creator, room_version, ts)

            create_content: dict[str, Any] = {"room_version": room_version}
            create_content.update(body.get("creation_content") or {})
            await self._append(
                room_id, etype="m.room.create", sender=creator, content=create_content,
                state_key="", ts=ts,
            )
            await self._append(
                room_id, etype="m.room.member", sender=creator,
                content={"membership": "join"}, state_key=creator, ts=ts,
            )

            power_levels = _default_power_levels(creator)
            power_levels.update(body.get("power_level_content_override") or {})
            await self._append(
                room_id, etype="m.room.power_levels", sender=creator,
                content=power_levels, state_key="", ts=ts,
            )
            await self._append(
                room_id, etype="m.room.join_rules", sender=creator,
                content={"join_rule": join_rule}, state_key="", ts=ts,
            )
            await self._append(
                room_id, etype="m.room.history_visibility", sender=creator,
                content={"history_visibility": _DEFAULT_HISTORY_VISIBILITY}, state_key="", ts=ts,
            )

            for state_event in body.get("initial_state") or []:
                await self._append(
                    room_id, etype=str(state_event["type"]), sender=creator,
                    content=dict(state_event.get("content", {})),
                    state_key=str(state_event.get("state_key", "")), ts=ts,
                )

            if isinstance(body.get("name"), str):
                await self._append(
                    room_id, etype="m.room.name", sender=creator,
                    content={"name": body["name"]}, state_key="", ts=ts,
                )
            if isinstance(body.get("topic"), str):
                await self._append(
                    room_id, etype="m.room.topic", sender=creator,
                    content={"topic": body["topic"]}, state_key="", ts=ts,
                )

            for invitee in body.get("invite") or []:
                await self._append(
                    room_id, etype="m.room.member", sender=creator,
                    content={"membership": "invite"}, state_key=str(invitee), ts=ts,
                )

        self._wake_syncs()
        return room_id

    # --- sending events ----------------------------------------------------

    async def send_message(
        self, room_id: str, sender: str, etype: str, content: dict[str, Any], txn_id: str
    ) -> str:
        await self._require_room(room_id)
        existing = await store.get_txn_event(self._db, sender, txn_id)
        if existing is not None:
            return existing

        state = await self._load_state(room_id)
        probe = Event(
            event_id="", room_id=room_id, type=etype, sender=sender, content=content,
            origin_server_ts=_now_ms(), depth=0, stream_ordering=0,
        )
        authrules.authorize(probe, state)

        async with self._db.transaction():
            event = await self._append(room_id, etype=etype, sender=sender, content=content)
            await store.put_txn_event(self._db, sender, txn_id, event.event_id)
        self._wake_syncs()
        return event.event_id

    async def send_state(
        self, room_id: str, sender: str, etype: str, state_key: str, content: dict[str, Any]
    ) -> str:
        await self._require_room(room_id)
        state = await self._load_state(room_id)
        probe = Event(
            event_id="", room_id=room_id, type=etype, sender=sender, content=content,
            origin_server_ts=_now_ms(), depth=0, stream_ordering=0, state_key=state_key,
        )
        authrules.authorize(probe, state)
        async with self._db.transaction():
            event = await self._append(
                room_id, etype=etype, sender=sender, content=content, state_key=state_key
            )
        self._wake_syncs()
        return event.event_id

    # --- membership --------------------------------------------------------

    async def _membership_change(
        self,
        room_id: str,
        sender: str,
        target: str,
        membership: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> str:
        await self._require_room(room_id)
        content: dict[str, Any] = {"membership": membership}
        if extra:
            content.update(extra)
        state = await self._load_state(room_id)
        probe = Event(
            event_id="", room_id=room_id, type="m.room.member", sender=sender, content=content,
            origin_server_ts=_now_ms(), depth=0, stream_ordering=0, state_key=target,
        )
        authrules.authorize(probe, state)
        async with self._db.transaction():
            event = await self._append(
                room_id, etype="m.room.member", sender=sender, content=content, state_key=target
            )
        self._wake_syncs()
        return event.event_id

    async def join(self, room_id: str, user_id: str) -> str:
        await self._membership_change(room_id, user_id, user_id, "join")
        return room_id

    async def leave(self, room_id: str, user_id: str) -> str:
        return await self._membership_change(room_id, user_id, user_id, "leave")

    async def invite(self, room_id: str, sender: str, target: str) -> str:
        return await self._membership_change(room_id, sender, target, "invite")

    async def kick(
        self, room_id: str, sender: str, target: str, reason: str | None = None
    ) -> str:
        extra = {"reason": reason} if reason else None
        return await self._membership_change(room_id, sender, target, "leave", extra=extra)

    async def ban(
        self, room_id: str, sender: str, target: str, reason: str | None = None
    ) -> str:
        extra = {"reason": reason} if reason else None
        return await self._membership_change(room_id, sender, target, "ban", extra=extra)

    async def unban(self, room_id: str, sender: str, target: str) -> str:
        return await self._membership_change(room_id, sender, target, "leave")

    # --- redaction ---------------------------------------------------------

    async def redact(
        self, room_id: str, sender: str, target_event_id: str, txn_id: str,
        reason: str | None = None,
    ) -> str:
        await self._require_room(room_id)
        existing = await store.get_txn_event(self._db, sender, txn_id)
        if existing is not None:
            return existing

        target = await store.get_event(self._db, room_id, target_event_id)
        if target is None:
            raise MatrixError(404, "M_NOT_FOUND", "Unknown event")

        state = await self._load_state(room_id)
        if authrules.membership_of(state, sender) != "join":
            raise MatrixError(403, "M_FORBIDDEN", "User is not in the room")
        # You may always redact your own event; otherwise you need the redact level.
        if sender != target.sender:
            if authrules.power_level_for(state, sender) < _redact_level(state):
                raise MatrixError(403, "M_FORBIDDEN", "Insufficient power level to redact")

        content = {"reason": reason} if reason else {}
        async with self._db.transaction():
            redaction = await self._append(
                room_id, etype="m.room.redaction", sender=sender, content=content,
                redacts=target_event_id,
            )
            await self._apply_redaction(target, redaction.event_id)
            await store.put_txn_event(self._db, sender, txn_id, redaction.event_id)
        self._wake_syncs()
        return redaction.event_id

    async def _apply_redaction(self, target: Event, redaction_event_id: str) -> None:
        redacted_content = versions.redact_content(target.type, target.content)
        unsigned = dict(target.unsigned or {})
        unsigned["redacted_because"] = redaction_event_id
        await store.update_event_content(
            self._db, target.event_id, json.dumps(redacted_content), json.dumps(unsigned)
        )

    # --- reads -------------------------------------------------------------

    async def get_state_events(self, room_id: str) -> list[dict[str, Any]]:
        await self._require_room(room_id)
        return [e.client_dict() for e in await store.get_current_state(self._db, room_id)]

    async def get_state_content(
        self, room_id: str, etype: str, state_key: str
    ) -> dict[str, Any]:
        await self._require_room(room_id)
        event = await store.get_state_event(self._db, room_id, etype, state_key)
        if event is None:
            raise MatrixError(404, "M_NOT_FOUND", "Event not found")
        return event.content

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any]:
        await self._require_room(room_id)
        event = await store.get_event(self._db, room_id, event_id)
        if event is None:
            raise MatrixError(404, "M_NOT_FOUND", "Event not found")
        return event.client_dict()

    async def get_messages(
        self, room_id: str, *, from_token: str | None, direction: str, limit: int
    ) -> dict[str, Any]:
        await self._require_room(room_id)
        limit = max(1, min(limit, 1000))
        if from_token is not None:
            try:
                start = int(from_token)
            except ValueError as exc:
                raise MatrixError(400, "M_INVALID_PARAM", "Invalid pagination token") from exc
        else:
            start = (await store.next_stream_ordering(self._db)) if direction == "b" else 0
        events = await store.get_messages(
            self._db, room_id, from_ordering=start, direction=direction, limit=limit
        )
        end = events[-1].stream_ordering if events else start
        return {
            "chunk": [e.client_dict() for e in events],
            "start": str(start),
            "end": str(end),
        }

    async def joined_rooms(self, user_id: str) -> list[str]:
        return await store.get_joined_rooms(self._db, user_id)

    async def joined_members(self, room_id: str) -> dict[str, dict[str, Any]]:
        await self._require_room(room_id)
        members = await store.get_joined_members(self._db, room_id)
        return {user_id: {} for user_id in members}


def _redact_level(state: AuthState) -> int:
    pl = state.get(("m.room.power_levels", ""))
    if pl is None or "redact" not in pl.content:
        return 50
    return int(pl.content["redact"])
