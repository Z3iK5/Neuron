# SPDX-License-Identifier: Apache-2.0
"""The ``/sync`` service.

Builds the Client-Server API ``/sync`` response from the event stream and the
E2EE relay. The sync token is composite — ``"<events>.<to_device>.<device_list>"``
— so each independent stream advances on its own. Initial sync (no ``since``)
returns each joined room's current state plus a recent timeline slice; incremental
sync returns what arrived after the token. Long-polling waits on the
:class:`StreamNotifier` until something changes.

Honest scope (HS-5): history visibility is treated as "shared"; presence and
account-data payloads are empty; ``device_lists.changed`` reports users sharing a
room with the syncer whose keys changed (slight over-reporting is harmless —
clients just re-query).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from neuron_server.errors import MatrixError
from neuron_server.storage import e2ee as e2ee_store
from neuron_server.storage import invites as invites_store
from neuron_server.storage import rooms as store
from neuron_server.storage.database import Database
from neuron_server.sync.notifier import StreamNotifier

_TIMELINE_LIMIT = 20
_TO_DEVICE_LIMIT = 100
_MAX_TIMEOUT_MS = 60_000

_STRIPPED_STATE_TYPES = frozenset(
    {
        "m.room.create",
        "m.room.join_rules",
        "m.room.name",
        "m.room.avatar",
        "m.room.topic",
        "m.room.canonical_alias",
        "m.room.encryption",
        "m.room.member",
    }
)


@dataclass(frozen=True)
class _Token:
    events: int | None  # None on initial sync
    to_device: int
    device_list: int
    invites: int = 0


class SyncService:
    """Produces ``/sync`` responses for clients and bots."""

    def __init__(self, db: Database, notifier: StreamNotifier) -> None:
        self._db = db
        self._notifier = notifier

    async def sync(
        self, user_id: str, device_id: str, *, since: str | None, timeout_ms: int
    ) -> dict[str, Any]:
        token = self._parse_token(since)

        body, changed = await self._build(user_id, device_id, token)
        if token.events is not None and not changed and timeout_ms > 0:
            await self._notifier.wait(min(timeout_ms, _MAX_TIMEOUT_MS) / 1000.0)
            body, _ = await self._build(user_id, device_id, token)
        return body

    @staticmethod
    def _parse_token(since: str | None) -> _Token:
        if not since:
            return _Token(events=None, to_device=0, device_list=0)
        parts = since.split(".")
        try:
            events = int(parts[0])
            to_device = int(parts[1]) if len(parts) > 1 else 0
            device_list = int(parts[2]) if len(parts) > 2 else 0
            invites = int(parts[3]) if len(parts) > 3 else 0
        except ValueError as exc:
            raise MatrixError(400, "M_INVALID_PARAM", "Invalid sync token") from exc
        return _Token(
            events=events, to_device=to_device, device_list=device_list, invites=invites
        )

    async def _build(
        self, user_id: str, device_id: str, token: _Token
    ) -> tuple[dict[str, Any], bool]:
        initial = token.events is None
        current_events = await store.get_max_stream_ordering(self._db)
        memberships = await store.get_user_memberships(self._db, user_id)

        join: dict[str, Any] = {}
        invite: dict[str, Any] = {}
        leave: dict[str, Any] = {}
        changed = False

        for room_id, membership in memberships:
            if membership == "join":
                section, room_changed = await self._joined_room(room_id, token.events, initial)
                if initial or room_changed:
                    join[room_id] = section
                    changed = changed or room_changed
            elif membership == "invite":
                include, section = await self._invited_room(
                    room_id, user_id, token.events, initial
                )
                if include:
                    invite[room_id] = section
                    changed = changed or not initial
            elif membership in ("leave", "ban") and not initial:
                include, section = await self._left_room(room_id, user_id, token.events)
                if include:
                    leave[room_id] = section
                    changed = True

        # Invites to rooms hosted by other servers (received over federation).
        new_invites = await self._federated_invites(user_id, token.invites, invite)
        if new_invites > token.invites:
            changed = changed or not initial

        to_device_events, new_to_device = await self._to_device(
            user_id, device_id, token.to_device
        )
        otk_counts = await e2ee_store.count_one_time_keys(self._db, user_id, device_id)
        device_changed, new_device_list = await self._device_lists(
            user_id, token.device_list, initial
        )

        if to_device_events or device_changed:
            changed = True

        next_batch = f"{current_events}.{new_to_device}.{new_device_list}.{new_invites}"
        body = {
            "next_batch": next_batch,
            "rooms": {"join": join, "invite": invite, "leave": leave, "knock": {}},
            "presence": {"events": []},
            "account_data": {"events": []},
            "to_device": {"events": to_device_events},
            "device_lists": {"changed": device_changed, "left": []},
            "device_one_time_keys_count": otk_counts,
        }
        return body, changed

    async def _to_device(
        self, user_id: str, device_id: str, since: int
    ) -> tuple[list[dict[str, Any]], int]:
        # Messages up to the acknowledged position have been received — clear them.
        if since > 0:
            await e2ee_store.delete_to_device_up_to(self._db, user_id, device_id, since)
        pending = await e2ee_store.get_to_device(
            self._db, user_id, device_id, since, _TO_DEVICE_LIMIT
        )
        events = [message for _, message in pending]
        new_pos = pending[-1][0] if pending else since
        return events, new_pos

    async def _device_lists(
        self, user_id: str, since: int, initial: bool
    ) -> tuple[list[str], int]:
        new_pos = await e2ee_store.max_device_list_stream(self._db)
        if initial:
            return [], new_pos
        changed_users = await e2ee_store.get_device_list_changes_after(self._db, since)
        if not changed_users:
            return [], new_pos
        sharing = set(await store.get_users_sharing_room(self._db, user_id))
        return [u for u in changed_users if u in sharing], new_pos

    async def _joined_room(
        self, room_id: str, since: int | None, initial: bool
    ) -> tuple[dict[str, Any], bool]:
        if initial:
            timeline = await store.get_recent_events(self._db, room_id, _TIMELINE_LIMIT)
            state = await store.get_current_state(self._db, room_id)
            prev = (timeline[0].stream_ordering - 1) if timeline else 0
            limited = len(timeline) >= _TIMELINE_LIMIT
            room_changed = True
        else:
            after = since or 0
            fetched = await store.get_events_after(self._db, room_id, after, _TIMELINE_LIMIT + 1)
            limited = len(fetched) > _TIMELINE_LIMIT
            timeline = fetched[:_TIMELINE_LIMIT]
            state = []
            prev = after
            room_changed = len(timeline) > 0

        section = {
            "timeline": {
                "events": [e.client_dict() for e in timeline],
                "limited": limited,
                "prev_batch": str(prev),
            },
            "state": {"events": [e.client_dict() for e in state]},
            "account_data": {"events": []},
            "ephemeral": {"events": []},
        }
        return section, room_changed

    async def _federated_invites(
        self, user_id: str, since_invites: int, invite: dict[str, Any]
    ) -> int:
        """Add invites to remote-hosted rooms into ``invite``; return the max
        invite stream position seen."""
        pending = await invites_store.list_pending_invites(self._db, user_id)
        highest = since_invites
        for entry in pending:
            highest = max(highest, entry.stream_id)
            stripped = list(entry.invite_state)
            event = entry.event
            stripped.append(
                {
                    "type": event.get("type"),
                    "state_key": event.get("state_key"),
                    "sender": event.get("sender"),
                    "content": event.get("content"),
                }
            )
            invite[entry.room_id] = {"invite_state": {"events": stripped}}
        return highest

    async def _invited_room(
        self, room_id: str, user_id: str, since: int | None, initial: bool
    ) -> tuple[bool, dict[str, Any]]:
        member = await store.get_state_event(self._db, room_id, "m.room.member", user_id)
        if member is None:
            return False, {}
        if not initial and member.stream_ordering <= (since or 0):
            return False, {}
        state = await store.get_current_state(self._db, room_id)
        stripped = [e.client_dict() for e in state if e.type in _STRIPPED_STATE_TYPES]
        return True, {"invite_state": {"events": stripped}}

    async def _left_room(
        self, room_id: str, user_id: str, since: int | None
    ) -> tuple[bool, dict[str, Any]]:
        member = await store.get_state_event(self._db, room_id, "m.room.member", user_id)
        if member is None or member.stream_ordering <= (since or 0):
            return False, {}
        section = {
            "timeline": {
                "events": [member.client_dict()],
                "limited": False,
                "prev_batch": str(since or 0),
            },
            "state": {"events": []},
            "account_data": {"events": []},
        }
        return True, section
