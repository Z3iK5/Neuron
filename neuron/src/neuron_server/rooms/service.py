# SPDX-License-Identifier: Apache-2.0
"""Room domain service: create rooms, send events, moderate, read state/history.

Single-server model: events form a linear DAG, so the room's *current state* is
the authorization context for each new event (no state resolution — that is part
of the federation epic, HS-7). Every client-originated event is checked against
:mod:`neuron_server.rooms.authrules` before it is persisted.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from neuron_server.crypto.event_hashing import add_hashes_and_signatures, compute_event_id
from neuron_server.crypto.signing import SigningKey
from neuron_server.errors import MatrixError
from neuron_server.rooms import authrules, versions
from neuron_server.rooms.authrules import AuthState
from neuron_server.rooms.events import Event, generate_room_id
from neuron_server.storage import accounts
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
        self,
        db: Database,
        server_name: str,
        signing_key: SigningKey,
        notify: Callable[[], None] | None = None,
        federation_sender: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._server_name = server_name
        self._signing_key = signing_key
        self._notify = notify
        self._federation_sender = federation_sender

    async def _propagate(self, room_id: str, event: Event) -> None:
        if self._federation_sender is not None:
            await self._federation_sender(room_id, event.pdu_dict())

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
        """Persist a new event, updating current state and membership. No auth check.

        Builds a proper federation PDU: selects ``prev_events`` (the room's forward
        extremity) and ``auth_events``, computes the content hash, derives the
        reference-hash event ID, and signs the event with the server key.
        """
        stream = await store.next_stream_ordering(self._db)
        extremity = await store.get_forward_extremity(self._db, room_id)
        prev_events = [extremity.event_id] if extremity is not None else []
        depth = (extremity.depth + 1) if extremity is not None else 1

        state = {} if etype == "m.room.create" else await self._load_state(room_id)
        auth_events = authrules.select_auth_event_ids(etype, state_key, sender, content, state)

        pdu: dict[str, Any] = {
            "room_id": room_id,
            "type": etype,
            "sender": sender,
            "content": content,
            "origin_server_ts": ts if ts is not None else _now_ms(),
            "depth": depth,
            "prev_events": prev_events,
            "auth_events": auth_events,
        }
        if state_key is not None:
            pdu["state_key"] = state_key
        pdu = add_hashes_and_signatures(
            pdu, server_name=self._server_name, signing_key=self._signing_key
        )
        event = Event(
            event_id=compute_event_id(pdu),
            room_id=room_id,
            type=etype,
            sender=sender,
            content=content,
            origin_server_ts=int(pdu["origin_server_ts"]),
            depth=depth,
            stream_ordering=stream,
            state_key=state_key,
            unsigned=unsigned,
            redacts=redacts,
            auth_events=auth_events,
            prev_events=prev_events,
            hashes=pdu["hashes"],
            signatures=pdu["signatures"],
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
        # A blocked room is closed to client traffic (sends, joins, reads). Admin
        # inspection goes through AdminService/storage directly and is unaffected;
        # admin_delete_room checks existence via store.get_room to bypass this.
        if await store.is_room_blocked(self._db, room_id):
            raise MatrixError(403, "M_FORBIDDEN", "This room has been blocked on this server")
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

        # Shadow-banned senders: silently accept the message (return a real-looking
        # event id, dedupe the txn) but never persist or propagate it, so it is
        # invisible to everyone else. Matches Synapse's shadow-ban semantics.
        sender_row = await accounts.get_user(self._db, sender)
        if sender_row is not None and sender_row.shadow_banned:
            fake_id = "$" + secrets.token_urlsafe(24)
            await store.put_txn_event(self._db, sender, txn_id, fake_id)
            return fake_id

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
        await self._propagate(room_id, event)
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
        await self._propagate(room_id, event)
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

        # Room v11 (MSC2174) carries the redaction target inside ``content``.
        content: dict[str, Any] = {"redacts": target_event_id}
        if reason:
            content["reason"] = reason
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

    # --- server-authority operations (used by the Admin API) ---------------

    async def admin_make_room_admin(self, room_id: str, user_id: str) -> None:
        """Grant ``user_id`` power level 100 in the room (bypasses normal auth)."""
        room = await self._require_room(room_id)
        state = await self._load_state(room_id)
        pl_event = state.get(("m.room.power_levels", ""))
        content = dict(pl_event.content) if pl_event else _default_power_levels(room.creator)
        users = dict(content.get("users", {}))
        users[user_id] = 100
        content["users"] = users
        async with self._db.transaction():
            await self._append(
                room_id, etype="m.room.power_levels", sender=room.creator,
                content=content, state_key="",
            )
        self._wake_syncs()

    async def admin_force_join(self, room_id: str, user_id: str) -> None:
        """Force ``user_id`` to join the room (bypasses normal auth)."""
        await self._require_room(room_id)
        async with self._db.transaction():
            await self._append(
                room_id, etype="m.room.member", sender=user_id,
                content={"membership": "join"}, state_key=user_id,
            )
        self._wake_syncs()

    async def admin_force_leave(self, room_id: str, user_id: str) -> None:
        """Force ``user_id`` out of the room (bypasses power-level checks)."""
        async with self._db.transaction():
            await self._append(
                room_id, etype="m.room.member", sender=user_id,
                content={"membership": "leave"}, state_key=user_id,
            )
        self._wake_syncs()

    async def admin_delete_room(
        self, room_id: str, *, purge: bool = True, block: bool = False, by: str | None = None
    ) -> dict[str, Any]:
        """Force every member out and (optionally) purge the room's data / block it.

        Synchronous — correct at desktop scale. Returns the kicked users.
        """
        # Bypass the block guard in _require_room so a blocked room can still be deleted.
        if await store.get_room(self._db, room_id) is None:
            raise MatrixError(404, "M_NOT_FOUND", "Unknown room")
        kicked = await store.get_joined_members(self._db, room_id)
        for member in kicked:
            await self.admin_force_leave(room_id, member)
        if purge:
            async with self._db.transaction():
                await store.purge_room(self._db, room_id)
        elif block:
            await store.set_room_blocked(self._db, room_id, True, by=by, ts=_now_ms())
        self._wake_syncs()
        return {"kicked_users": kicked}

    async def admin_redact_user_events(
        self, user_id: str, *, room_id: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Redact a user's message events (server authority). Returns total + failures."""
        targets = await store.get_redactable_events_by_sender(
            self._db, user_id, room_id=room_id, limit=limit
        )
        failed: list[str] = []
        for rid, eid in targets:
            try:
                await self._admin_redact_one(rid, eid)
            except Exception:  # noqa: BLE001 - record and continue the bulk job
                failed.append(eid)
        self._wake_syncs()
        return {"total": len(targets), "failed": failed}

    async def _admin_redact_one(self, room_id: str, event_id: str) -> None:
        target = await store.get_event(self._db, room_id, event_id)
        if target is None:
            raise MatrixError(404, "M_NOT_FOUND", "Unknown event")
        room = await store.get_room(self._db, room_id)
        redactor = room.creator if room is not None else target.sender
        async with self._db.transaction():
            redaction = await self._append(
                room_id, etype="m.room.redaction", sender=redactor,
                content={"redacts": event_id}, redacts=event_id,
            )
            await self._apply_redaction(target, redaction.event_id)

    # --- federated membership (resident side: make_join / send_join) --------

    async def make_join_template(self, room_id: str, user_id: str) -> dict[str, Any]:
        """Build the unsigned join-event template a remote server completes."""
        room = await self._require_room(room_id)
        state = await self._load_state(room_id)
        join_rules = state.get(("m.room.join_rules", ""))
        join_rule = join_rules.content.get("join_rule") if join_rules else "invite"
        if join_rule != "public" and authrules.membership_of(state, user_id) != "invite":
            raise MatrixError(403, "M_FORBIDDEN", "Not invited and the room is not public")

        extremity = await store.get_forward_extremity(self._db, room_id)
        prev_events = [extremity.event_id] if extremity is not None else []
        depth = (extremity.depth + 1) if extremity is not None else 1
        content = {"membership": "join"}
        auth_events = authrules.select_auth_event_ids(
            "m.room.member", user_id, user_id, content, state
        )
        return {
            "room_version": room.room_version,
            "event": {
                "room_id": room_id,
                "type": "m.room.member",
                "sender": user_id,
                "state_key": user_id,
                "content": content,
                "depth": depth,
                "prev_events": prev_events,
                "auth_events": auth_events,
                "origin_server_ts": _now_ms(),
            },
        }

    async def apply_external_join(
        self, room_id: str, pdu: dict[str, Any]
    ) -> tuple[list[Event], list[Event]]:
        """Authorise and persist a remote server's signed join event.

        Returns ``(current_state, auth_chain)`` for the send_join response.
        """
        await self._require_room(room_id)
        sender = pdu.get("sender")
        if (
            pdu.get("type") != "m.room.member"
            or not isinstance(sender, str)
            or pdu.get("state_key") != sender
            or (pdu.get("content") or {}).get("membership") != "join"
        ):
            raise MatrixError(400, "M_INVALID_PARAM", "Not a valid join event")

        event_id = compute_event_id(pdu)
        state = await self._load_state(room_id)
        probe = Event(
            event_id=event_id, room_id=room_id, type="m.room.member", sender=sender,
            content=dict(pdu["content"]), origin_server_ts=int(pdu["origin_server_ts"]),
            depth=int(pdu["depth"]), stream_ordering=0, state_key=sender,
        )
        authrules.authorize(probe, state)

        async with self._db.transaction():
            stream = await store.next_stream_ordering(self._db)
            event = Event(
                event_id=event_id, room_id=room_id, type="m.room.member", sender=sender,
                content=dict(pdu["content"]), origin_server_ts=int(pdu["origin_server_ts"]),
                depth=int(pdu["depth"]), stream_ordering=stream, state_key=sender,
                auth_events=list(pdu.get("auth_events", [])),
                prev_events=list(pdu.get("prev_events", [])),
                hashes=pdu.get("hashes"), signatures=pdu.get("signatures"),
            )
            await store.insert_event(self._db, event)
            await store.update_current_state(self._db, room_id, "m.room.member", sender, event_id)
            await store.set_membership(self._db, room_id, sender, "join")
        self._wake_syncs()

        current_state = await store.get_current_state(self._db, room_id)
        auth_seed: list[str] = []
        for member in current_state:
            auth_seed.extend(member.auth_events)
        auth_chain = await store.get_auth_chain(self._db, room_id, auth_seed)
        return current_state, auth_chain

    async def make_leave_template(self, room_id: str, user_id: str) -> dict[str, Any]:
        """Build the unsigned leave-event template a remote server completes."""
        room = await self._require_room(room_id)
        state = await self._load_state(room_id)
        extremity = await store.get_forward_extremity(self._db, room_id)
        prev_events = [extremity.event_id] if extremity is not None else []
        depth = (extremity.depth + 1) if extremity is not None else 1
        content = {"membership": "leave"}
        auth_events = authrules.select_auth_event_ids(
            "m.room.member", user_id, user_id, content, state
        )
        return {
            "room_version": room.room_version,
            "event": {
                "room_id": room_id,
                "type": "m.room.member",
                "sender": user_id,
                "state_key": user_id,
                "content": content,
                "depth": depth,
                "prev_events": prev_events,
                "auth_events": auth_events,
                "origin_server_ts": _now_ms(),
            },
        }

    async def apply_external_leave(self, room_id: str, pdu: dict[str, Any]) -> None:
        """Authorise and persist a remote server's signed leave event."""
        await self._require_room(room_id)
        sender = pdu.get("sender")
        if (
            pdu.get("type") != "m.room.member"
            or not isinstance(sender, str)
            or pdu.get("state_key") != sender
            or (pdu.get("content") or {}).get("membership") != "leave"
        ):
            raise MatrixError(400, "M_INVALID_PARAM", "Not a valid leave event")

        event_id = compute_event_id(pdu)
        state = await self._load_state(room_id)
        probe = Event(
            event_id=event_id, room_id=room_id, type="m.room.member", sender=sender,
            content=dict(pdu["content"]), origin_server_ts=int(pdu["origin_server_ts"]),
            depth=int(pdu["depth"]), stream_ordering=0, state_key=sender,
        )
        authrules.authorize(probe, state)

        async with self._db.transaction():
            if await store.get_event(self._db, room_id, event_id) is None:
                stream = await store.next_stream_ordering(self._db)
                await store.insert_event(self._db, Event.from_pdu(pdu, event_id, stream))
            await store.update_current_state(self._db, room_id, "m.room.member", sender, event_id)
            await store.set_membership(self._db, room_id, sender, "leave")
        self._wake_syncs()

    # State types shared with an invited user's server so it can render the room.
    _INVITE_STATE_KEYS = (
        ("m.room.create", ""),
        ("m.room.join_rules", ""),
        ("m.room.name", ""),
        ("m.room.canonical_alias", ""),
        ("m.room.avatar", ""),
        ("m.room.encryption", ""),
    )

    def _stripped_invite_state(self, state: AuthState, inviter: str) -> list[dict[str, Any]]:
        keys = [*self._INVITE_STATE_KEYS, ("m.room.member", inviter)]
        stripped: list[dict[str, Any]] = []
        for key in keys:
            event = state.get(key)
            if event is not None:
                stripped.append(
                    {
                        "type": event.type,
                        "state_key": event.state_key or "",
                        "sender": event.sender,
                        "content": event.content,
                    }
                )
        return stripped

    async def build_invite(
        self, room_id: str, sender: str, target: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Authorise and sign an invite event (without persisting it).

        Returns ``(invite_pdu, invite_room_state)`` — the latter is the stripped
        state shared with the invited user's server.
        """
        await self._require_room(room_id)
        state = await self._load_state(room_id)
        content = {"membership": "invite"}
        probe = Event(
            event_id="", room_id=room_id, type="m.room.member", sender=sender,
            content=content, origin_server_ts=_now_ms(), depth=0, stream_ordering=0,
            state_key=target,
        )
        authrules.authorize(probe, state)

        extremity = await store.get_forward_extremity(self._db, room_id)
        prev_events = [extremity.event_id] if extremity is not None else []
        depth = (extremity.depth + 1) if extremity is not None else 1
        auth_events = authrules.select_auth_event_ids(
            "m.room.member", target, sender, content, state
        )
        pdu: dict[str, Any] = {
            "room_id": room_id,
            "type": "m.room.member",
            "sender": sender,
            "state_key": target,
            "content": content,
            "depth": depth,
            "prev_events": prev_events,
            "auth_events": auth_events,
            "origin_server_ts": _now_ms(),
        }
        pdu = add_hashes_and_signatures(
            pdu, server_name=self._server_name, signing_key=self._signing_key
        )
        return pdu, self._stripped_invite_state(state, sender)

    async def apply_remote_event(self, pdu: dict[str, Any]) -> bool:
        """Apply an event received over federation to our copy of the room.

        Returns ``True`` if the event was stored. Skips events for rooms we don't
        participate in, duplicates, and events the auth rules reject against our
        current state. Full DAG/state-resolution handling is still to come, so this
        is best-effort for forks; it is exact for the common linear case.
        """
        room_id = str(pdu.get("room_id", ""))
        if await store.get_room(self._db, room_id) is None:
            return False
        event_id = compute_event_id(pdu)
        if await store.get_event(self._db, room_id, event_id) is not None:
            return True

        state = await self._load_state(room_id)
        probe = Event.from_pdu(pdu, event_id, 0)
        try:
            authrules.authorize(probe, state)
        except MatrixError:
            return False

        async with self._db.transaction():
            stream = await store.next_stream_ordering(self._db)
            event = Event.from_pdu(pdu, event_id, stream)
            await store.insert_event(self._db, event)
            if event.state_key is not None:
                await store.update_current_state(
                    self._db, room_id, event.type, event.state_key, event_id
                )
                if event.type == "m.room.member":
                    await store.set_membership(
                        self._db, room_id, event.state_key,
                        str(event.content.get("membership")),
                    )
        self._wake_syncs()
        return True

    async def apply_invite(self, room_id: str, pdu: dict[str, Any]) -> str:
        """Persist an invite event (already authorised in :meth:`build_invite`)."""
        await self._require_room(room_id)
        event_id = compute_event_id(pdu)
        target = str(pdu["state_key"])
        async with self._db.transaction():
            if await store.get_event(self._db, room_id, event_id) is None:
                stream = await store.next_stream_ordering(self._db)
                await store.insert_event(self._db, Event.from_pdu(pdu, event_id, stream))
            await store.update_current_state(self._db, room_id, "m.room.member", target, event_id)
            await store.set_membership(self._db, room_id, target, "invite")
        self._wake_syncs()
        return event_id


def _redact_level(state: AuthState) -> int:
    pl = state.get(("m.room.power_levels", ""))
    if pl is None or "redact" not in pl.content:
        return 50
    return int(pl.content["redact"])
