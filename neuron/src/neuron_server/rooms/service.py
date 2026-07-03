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
from collections.abc import Awaitable, Callable
from typing import Any

from neuron_core import get_logger
from neuron_server.clock import now_ms
from neuron_server.crypto.event_hashing import add_hashes_and_signatures, compute_event_id
from neuron_server.crypto.signing import SigningKey
from neuron_server.errors import MatrixError
from neuron_server.federation.validation import domain_of
from neuron_server.rooms import authrules, state_resolution, versions
from neuron_server.rooms.authrules import AuthState
from neuron_server.rooms.events import Event, generate_room_id
from neuron_server.storage import accounts
from neuron_server.storage import rooms as store
from neuron_server.storage.database import Database

_logger = get_logger(__name__)

_DEFAULT_HISTORY_VISIBILITY = "shared"



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
        federation_sender: Callable[..., Awaitable[None]] | None = None,
        state_res_v2: bool = False,
    ) -> None:
        self._db = db
        self._server_name = server_name
        self._signing_key = signing_key
        self._notify = notify
        self._federation_sender = federation_sender
        self._state_res_v2 = state_res_v2

    async def _propagate(
        self, room_id: str, event: Event, *, extra_destinations: set[str] | None = None
    ) -> None:
        """Send a locally-created event to the room's remote participants.

        Best-effort: federation propagation must never break the local action, so a
        failure here is logged and swallowed. We can only sign events as our own
        server, so an event whose sender is on another server (e.g. a self-leave we
        forced on a remote user) is kept local — a remote peer would reject its
        signature anyway. ``extra_destinations`` lets a membership-removal caller
        reach a server that has just dropped out of the room's joined members.
        """
        if self._federation_sender is None:
            return
        if domain_of(event.sender) != self._server_name:
            return
        try:
            await self._federation_sender(
                room_id, event.pdu_dict(), extra_destinations=extra_destinations
            )
        except Exception as exc:  # noqa: BLE001 - propagation is best-effort
            _logger.warning("federation propagation failed for %s: %s", event.event_id, exc)

    async def _remote_destinations(self, room_id: str) -> set[str]:
        """Snapshot the other servers with a joined member in ``room_id``.

        Captured *before* a membership change so a kicked/banned/departing remote
        member's server is still notified after it leaves the joined-member set.
        """
        members = await store.get_joined_members(self._db, room_id)
        return {
            domain_of(user_id)
            for user_id in members
            if domain_of(user_id) != self._server_name
        }

    def _wake_syncs(self) -> None:
        if self._notify is not None:
            self._notify()

    async def is_shadow_banned(self, user_id: str) -> bool:
        """Whether ``user_id`` is shadow-banned.

        A shadow-banned user's content-producing actions (messages, state events,
        redactions, invites) are silently accepted but never take effect, so the
        user cannot tell they have been banned. Their own membership (join/leave)
        is left alone for the same reason.
        """
        row = await accounts.get_user(self._db, user_id)
        return row is not None and row.shadow_banned

    @staticmethod
    def _fake_event_id() -> str:
        """A plausible-looking event id returned for a silently-dropped action."""
        return "$" + secrets.token_urlsafe(24)

    # --- internals ---------------------------------------------------------

    async def _load_state(self, room_id: str) -> AuthState:
        events = await store.get_current_state(self._db, room_id)
        return {(e.type, e.state_key or ""): e for e in events}

    def _resolved_auth_state(self, state: AuthState) -> AuthState:
        """Route the authorization state through state resolution v2 (HS-7 6c).

        The room has a single forward extremity today, so resolving one state map
        is an exact no-op — but routing inbound-federation authorization through
        :func:`state_resolution.resolve` keeps the algorithm on the live path
        (not dead code) and marks the seam where multi-extremity resolution plugs
        in once a ``forward_extremities`` table + multi-``prev_event`` appends
        exist. Gated by the ``state_res_v2`` setting (default off).
        """
        state_map = {key: ev.event_id for key, ev in state.items()}
        event_map = {ev.event_id: ev for ev in state.values()}
        resolved = state_resolution.resolve([state_map], event_map)
        return {key: event_map[eid] for key, eid in resolved.items()}

    async def _dag_position(self, room_id: str) -> tuple[list[str], int]:
        """The ``prev_events`` and ``depth`` for a new event at the room's tip."""
        extremity = await store.get_forward_extremity(self._db, room_id)
        prev_events = [extremity.event_id] if extremity is not None else []
        depth = (extremity.depth + 1) if extremity is not None else 1
        return prev_events, depth

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
        prev_events, depth = await self._dag_position(room_id)

        state = {} if etype == "m.room.create" else await self._load_state(room_id)
        auth_events = authrules.select_auth_event_ids(etype, state_key, sender, content, state)

        pdu: dict[str, Any] = {
            "room_id": room_id,
            "type": etype,
            "sender": sender,
            "content": content,
            "origin_server_ts": ts if ts is not None else now_ms(),
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
        ts = now_ms()
        # A shadow-banned creator still gets their (private) room so they can't tell
        # they are banned, but their invites are dropped — otherwise the createRoom
        # invite list would be an open bypass of the invite shadow-ban.
        invitees = [] if await self.is_shadow_banned(creator) else (body.get("invite") or [])

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

            for invitee in invitees:
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
        if await self.is_shadow_banned(sender):
            fake_id = self._fake_event_id()
            await store.put_txn_event(self._db, sender, txn_id, fake_id)
            return fake_id

        state = await self._load_state(room_id)
        probe = Event(
            event_id="", room_id=room_id, type=etype, sender=sender, content=content,
            origin_server_ts=now_ms(), depth=0, stream_ordering=0,
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
        # Shadow-banned senders: silently no-op (state changes stay invisible).
        if await self.is_shadow_banned(sender):
            return self._fake_event_id()
        state = await self._load_state(room_id)
        probe = Event(
            event_id="", room_id=room_id, type=etype, sender=sender, content=content,
            origin_server_ts=now_ms(), depth=0, stream_ordering=0, state_key=state_key,
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
            origin_server_ts=now_ms(), depth=0, stream_ordering=0, state_key=target,
        )
        authrules.authorize(probe, state)

        # Snapshot remote destinations before the change: a leave/ban/kick removes
        # the target from the joined-member set, so afterwards the sender would no
        # longer compute the target's server as a destination.
        pre = await self._remote_destinations(room_id)
        if membership in {"leave", "ban", "invite"}:
            target_domain = domain_of(target)
            if target_domain != self._server_name:
                pre.add(target_domain)

        async with self._db.transaction():
            event = await self._append(
                room_id, etype="m.room.member", sender=sender, content=content, state_key=target
            )
        self._wake_syncs()
        await self._propagate(room_id, event, extra_destinations=pre)
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

        # Shadow-banned senders: silently no-op (the target stays un-redacted).
        if await self.is_shadow_banned(sender):
            fake_id = self._fake_event_id()
            await store.put_txn_event(self._db, sender, txn_id, fake_id)
            return fake_id

        target = await store.get_event(self._db, room_id, target_event_id)
        if target is None:
            raise MatrixError(404, "M_NOT_FOUND", "Unknown event")

        state = await self._load_state(room_id)
        if authrules.membership_of(state, sender) != "join":
            raise MatrixError(403, "M_FORBIDDEN", "User is not in the room")
        # You may always redact your own event; otherwise you need the redact level.
        if sender != target.sender:
            if authrules.power_level_for(state, sender) < authrules.named_level(state, "redact"):
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
        # Make sure the redaction reaches the redacted author's server even if they
        # are no longer a joined member (e.g. already kicked/banned).
        pre = await self._remote_destinations(room_id)
        if domain_of(target.sender) != self._server_name:
            pre.add(domain_of(target.sender))
        await self._propagate(room_id, redaction, extra_destinations=pre)
        return redaction.event_id

    async def _apply_redaction(self, target: Event, redaction_event_id: str) -> None:
        redacted_content = versions.redact_content(target.type, target.content)
        unsigned = dict(target.unsigned or {})
        unsigned["redacted_because"] = redaction_event_id
        await store.update_event_content(
            self._db, target.event_id, json.dumps(redacted_content), json.dumps(unsigned)
        )

    def _may_apply_redaction(
        self, state: AuthState, redactor: str, target: Event | None
    ) -> bool:
        """Whether ``redactor`` may scrub ``target`` (and it isn't already scrubbed).

        Mirrors the local :meth:`redact` rule: you may always redact your own event,
        otherwise you need the room's redact power level. Applied to events received
        over federation so a remote server cannot scrub arbitrary content via a
        low-power member.
        """
        if target is None or (target.unsigned or {}).get("redacted_because"):
            return False
        if redactor == target.sender:
            return True
        return authrules.power_level_for(state, redactor) >= authrules.named_level(state, "redact")

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

    async def _require_joined(self, room_id: str, user_id: str) -> AuthState:
        """Require ``user_id`` to be a joined member; return the current state."""
        state = await self._load_state(room_id)
        if authrules.membership_of(state, user_id) != "join":
            raise MatrixError(403, "M_FORBIDDEN", "User is not in the room")
        return state

    async def get_event_context(
        self, room_id: str, requester: str, event_id: str, *, limit: int
    ) -> dict[str, Any]:
        """Return an event with surrounding timeline events and room state.

        ``start``/``end`` are stream-ordering tokens compatible with the
        ``from``/``to`` tokens of :meth:`get_messages`. ``state`` is the room's
        *current* state (historical state-at-event is not tracked; at this
        server's scale the current state is an acceptable approximation).
        """
        await self._require_room(room_id)
        state = await self._require_joined(room_id, requester)
        event = await store.get_event(self._db, room_id, event_id)
        if event is None:
            raise MatrixError(404, "M_NOT_FOUND", "Event not found")

        limit = max(0, min(limit, 1000))
        before_limit = limit // 2
        after_limit = limit - before_limit
        events_before = (
            await store.get_messages(
                self._db, room_id, from_ordering=event.stream_ordering,
                direction="b", limit=before_limit,
            )
            if before_limit
            else []
        )
        events_after = (
            await store.get_messages(
                self._db, room_id, from_ordering=event.stream_ordering,
                direction="f", limit=after_limit,
            )
            if after_limit
            else []
        )
        # events_before is reverse-chronological (per spec), so its last element is
        # the oldest event returned — the right `from` for paginating further back.
        start = events_before[-1].stream_ordering if events_before else event.stream_ordering
        end = events_after[-1].stream_ordering if events_after else event.stream_ordering
        return {
            "event": event.client_dict(),
            "events_before": [e.client_dict() for e in events_before],
            "events_after": [e.client_dict() for e in events_after],
            "start": str(start),
            "end": str(end),
            "state": [e.client_dict() for e in state.values()],
        }

    async def get_member_events(
        self,
        room_id: str,
        requester: str,
        *,
        membership: str | None = None,
        not_membership: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the room's ``m.room.member`` state events, optionally filtered."""
        await self._require_room(room_id)
        state = await self._require_joined(room_id, requester)
        chunk: list[dict[str, Any]] = []
        for (etype, _), event in sorted(state.items()):
            if etype != "m.room.member":
                continue
            value = str(event.content.get("membership"))
            if membership is not None and value != membership:
                continue
            if not_membership is not None and value == not_membership:
                continue
            chunk.append(event.client_dict())
        return chunk

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
            event = await self._append(
                room_id, etype="m.room.power_levels", sender=room.creator,
                content=content, state_key="",
            )
        self._wake_syncs()
        # sender is room.creator; _propagate keeps it local when the creator is on
        # another server (we could not sign it as them).
        await self._propagate(room_id, event)

    async def admin_force_join(self, room_id: str, user_id: str) -> None:
        """Force ``user_id`` to join the room (bypasses normal auth)."""
        await self._require_room(room_id)
        async with self._db.transaction():
            event = await self._append(
                room_id, etype="m.room.member", sender=user_id,
                content={"membership": "join"}, state_key=user_id,
            )
        self._wake_syncs()
        # A join is signed as the joining user, so this only propagates for a local
        # user_id (a remote user's join can only be authored by their own server).
        await self._propagate(room_id, event)

    async def admin_force_leave(self, room_id: str, user_id: str) -> None:
        """Force ``user_id`` out of the room (bypasses power-level checks)."""
        pre = await self._remote_destinations(room_id)
        target_domain = domain_of(user_id)
        if target_domain != self._server_name:
            pre.add(target_domain)
        async with self._db.transaction():
            event = await self._append(
                room_id, etype="m.room.member", sender=user_id,
                content={"membership": "leave"}, state_key=user_id,
            )
        self._wake_syncs()
        # A self-leave is signed as ``user_id``; _propagate keeps it local for a
        # remote target (admin_delete_room emits a creator-signed kick for those).
        await self._propagate(room_id, event, extra_destinations=pre)

    async def admin_delete_room(
        self, room_id: str, *, purge: bool = True, block: bool = False, by: str | None = None
    ) -> dict[str, Any]:
        """Force every member out and (optionally) purge the room's data / block it.

        Synchronous — correct at desktop scale. Returns the kicked users.
        """
        # Bypass the block guard in _require_room so a blocked room can still be deleted.
        room = await store.get_room(self._db, room_id)
        if room is None:
            raise MatrixError(404, "M_NOT_FOUND", "Unknown room")
        kicked = await store.get_joined_members(self._db, room_id)
        # Snapshot every remote member's server once: each removal shrinks the
        # joined-member set, so we must capture destinations before the loop.
        pre = {domain_of(m) for m in kicked if domain_of(m) != self._server_name}
        creator_is_local = domain_of(room.creator) == self._server_name
        local_members = [m for m in kicked if domain_of(m) == self._server_name]
        remote_members = [m for m in kicked if domain_of(m) != self._server_name]

        # Remove remote members FIRST, via creator-signed kicks, so the kicker (the
        # local room creator) is still joined on every remote copy when the kicks
        # arrive — a kick from a user who has already left would be rejected there.
        for member in remote_members:
            if creator_is_local:
                # A moderator-signed kick (sender = the local room creator) is
                # federation-valid, unlike a self-leave we cannot sign for them.
                async with self._db.transaction():
                    event = await self._append(
                        room_id, etype="m.room.member", sender=room.creator,
                        content={"membership": "leave"}, state_key=member,
                    )
                self._wake_syncs()
                await self._propagate(room_id, event, extra_destinations=pre)
            else:
                # No local authority to sign a kick for a remote member: tear the
                # room down locally only (no valid PDU we could produce).
                await self.admin_force_leave(room_id, member)
        # Then the local members (their self-leaves are validly signed by us).
        for member in local_members:
            await self.admin_force_leave(room_id, member)
        if purge:
            async with self._db.transaction():
                await store.purge_room(self._db, room_id)
        # Block and purge are independent (Synapse semantics): blocking after the
        # purge, since purge_room clears the room's blocked_rooms row.
        if block:
            await store.set_room_blocked(self._db, room_id, True, by=by, ts=now_ms())
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
        # sender is the room creator; _propagate keeps it local for remote-creator
        # rooms (we could not sign as them). The snapshot + the author's own domain
        # ensures the redaction still reaches a target whose server has already left
        # (the common "ban a remote spammer then redact their backlog" sequence).
        pre = await self._remote_destinations(room_id)
        if domain_of(target.sender) != self._server_name:
            pre.add(domain_of(target.sender))
        await self._propagate(room_id, redaction, extra_destinations=pre)

    # --- federated membership (resident side: make_join / send_join) --------

    async def _membership_template(
        self, room_id: str, user_id: str, membership: str
    ) -> dict[str, Any]:
        """Build the unsigned membership-event template a remote server completes."""
        room = await self._require_room(room_id)
        state = await self._load_state(room_id)
        if membership == "join":
            join_rules = state.get(("m.room.join_rules", ""))
            join_rule = join_rules.content.get("join_rule") if join_rules else "invite"
            if join_rule != "public" and authrules.membership_of(state, user_id) != "invite":
                raise MatrixError(403, "M_FORBIDDEN", "Not invited and the room is not public")

        prev_events, depth = await self._dag_position(room_id)
        content = {"membership": membership}
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
                "origin_server_ts": now_ms(),
            },
        }

    async def make_join_template(self, room_id: str, user_id: str) -> dict[str, Any]:
        """Build the unsigned join-event template a remote server completes."""
        return await self._membership_template(room_id, user_id, "join")

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
            or pdu.get("room_id") != room_id
            or not isinstance(sender, str)
            or pdu.get("state_key") != sender
            or (pdu.get("content") or {}).get("membership") != "join"
        ):
            raise MatrixError(400, "M_INVALID_PARAM", "Not a valid join event")

        event_id = compute_event_id(pdu)
        state = await self._load_state(room_id)
        authrules.authorize(Event.from_pdu(pdu, event_id, 0), state)

        # A retried send_join re-delivers the same signed event; the insert guard
        # keeps the endpoint idempotent instead of tripping the primary key.
        async with self._db.transaction():
            if await store.get_event(self._db, room_id, event_id) is None:
                stream = await store.next_stream_ordering(self._db)
                await store.insert_event(self._db, Event.from_pdu(pdu, event_id, stream))
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
        return await self._membership_template(room_id, user_id, "leave")

    async def apply_external_leave(self, room_id: str, pdu: dict[str, Any]) -> None:
        """Authorise and persist a remote server's signed leave event."""
        await self._require_room(room_id)
        sender = pdu.get("sender")
        if (
            pdu.get("type") != "m.room.member"
            or pdu.get("room_id") != room_id
            or not isinstance(sender, str)
            or pdu.get("state_key") != sender
            or (pdu.get("content") or {}).get("membership") != "leave"
        ):
            raise MatrixError(400, "M_INVALID_PARAM", "Not a valid leave event")

        event_id = compute_event_id(pdu)
        state = await self._load_state(room_id)
        authrules.authorize(Event.from_pdu(pdu, event_id, 0), state)

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
            content=content, origin_server_ts=now_ms(), depth=0, stream_ordering=0,
            state_key=target,
        )
        authrules.authorize(probe, state)

        prev_events, depth = await self._dag_position(room_id)
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
            "origin_server_ts": now_ms(),
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
        if self._state_res_v2:
            state = self._resolved_auth_state(state)
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
            elif event.type == "m.room.redaction" and event.redacts:
                # Apply a redaction received over federation to its target, so a
                # remotely-moderated message is actually scrubbed in our copy too.
                target = await store.get_event(self._db, room_id, event.redacts)
                if self._may_apply_redaction(state, event.sender, target):
                    await self._apply_redaction(target, event_id)  # type: ignore[arg-type]
            if event.type != "m.room.redaction":
                # The new event may itself be the target of a redaction that arrived
                # before it (out-of-order federation delivery); reconcile it now.
                pending = await store.get_redaction_for(self._db, room_id, event_id)
                if pending is not None and self._may_apply_redaction(
                    state, pending.sender, event
                ):
                    await self._apply_redaction(event, pending.event_id)
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
