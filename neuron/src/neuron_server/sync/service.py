# SPDX-License-Identifier: Apache-2.0
"""The ``/sync`` service.

Builds the Client-Server API ``/sync`` response from the event stream. Sync
tokens are the server-local ``stream_ordering`` position (as a string). Initial
sync (no ``since``) returns each joined room's current state plus a recent slice
of its timeline; incremental sync returns the events that arrived after the
token. Long-polling waits on the :class:`StreamNotifier` until something changes.

Honest scope (HS-3): ``to_device``, ``device_lists`` and ``account_data`` are
present but empty — they are populated by later phases (to-device/keys in HS-5).
History visibility is treated as "shared" (a joined user may read the room's
recent history); per-membership visibility is a later refinement.
"""

from __future__ import annotations

from typing import Any

from neuron_server.errors import MatrixError
from neuron_server.storage import rooms as store
from neuron_server.storage.database import Database
from neuron_server.sync.notifier import StreamNotifier

_TIMELINE_LIMIT = 20
_MAX_TIMEOUT_MS = 60_000

# State events included as "stripped state" for an invited room.
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


class SyncService:
    """Produces ``/sync`` responses for clients and bots."""

    def __init__(self, db: Database, notifier: StreamNotifier) -> None:
        self._db = db
        self._notifier = notifier

    async def sync(
        self, user_id: str, *, since: str | None, timeout_ms: int
    ) -> dict[str, Any]:
        since_ordering = self._parse_since(since)
        initial = since_ordering is None

        body, changed = await self._build(user_id, since_ordering, initial)
        if not initial and not changed and timeout_ms > 0:
            await self._notifier.wait(min(timeout_ms, _MAX_TIMEOUT_MS) / 1000.0)
            body, _ = await self._build(user_id, since_ordering, initial=False)
        return body

    @staticmethod
    def _parse_since(since: str | None) -> int | None:
        if since is None or since == "":
            return None
        try:
            return int(since)
        except ValueError as exc:
            raise MatrixError(400, "M_INVALID_PARAM", "Invalid sync token") from exc

    async def _build(
        self, user_id: str, since: int | None, initial: bool
    ) -> tuple[dict[str, Any], bool]:
        current_max = await store.get_max_stream_ordering(self._db)
        memberships = await store.get_user_memberships(self._db, user_id)

        join: dict[str, Any] = {}
        invite: dict[str, Any] = {}
        leave: dict[str, Any] = {}
        changed = False

        for room_id, membership in memberships:
            if membership == "join":
                section, room_changed = await self._joined_room(room_id, since, initial)
                if initial or room_changed:
                    join[room_id] = section
                    changed = changed or room_changed
            elif membership == "invite":
                include, section = await self._invited_room(room_id, user_id, since, initial)
                if include:
                    invite[room_id] = section
                    changed = changed or not initial
            elif membership in ("leave", "ban") and not initial:
                include, section = await self._left_room(room_id, user_id, since)
                if include:
                    leave[room_id] = section
                    changed = True

        body = {
            "next_batch": str(current_max),
            "rooms": {"join": join, "invite": invite, "leave": leave, "knock": {}},
            "presence": {"events": []},
            "account_data": {"events": []},
            "to_device": {"events": []},
            "device_lists": {"changed": [], "left": []},
            "device_one_time_keys_count": {},
        }
        return body, changed

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
