# SPDX-License-Identifier: Apache-2.0
"""Native Simplified Sliding Sync (MSC4186, successor to MSC3575).

This is the server-side sliding-window sync that Element X (and other modern
mobile clients) use instead of the classic long ``/sync``. Rather than returning
*every* room on every sync, the client asks for one or more sliding *lists* —
each a set of index ``ranges`` over the user's rooms sorted by recency — plus
explicit ``room_subscriptions``; the server returns only the rooms that fall in
those windows, with just the ``required_state`` and a bounded ``timeline`` slice
the client asked for.

Every underlying data source is shared verbatim with the classic ``/sync``
(:mod:`neuron_server.sync.service`): the event stream + current state
(:mod:`neuron_server.storage.rooms`), unread counts
(:mod:`neuron_server.storage.receipts`), the to-device inbox / device-list
changes / OTK counts (:mod:`neuron_server.storage.e2ee`), account data
(:mod:`neuron_server.storage.userdata`) and typing
(:class:`neuron_server.typing_state.TypingHandler`). This module only *reshapes*
that data into the MSC4186 response; it never adds a new source of truth.

Connection state (initial vs delta)
------------------------------------
A sliding-sync *connection* is keyed by ``(user_id, device_id, conn_id)``. The
server tracks, per connection, a monotonic ``pos`` counter and a snapshot of what
it has already sent (which rooms, the last timeline stream position per room, and
the per-extension stream positions). The **first** request (no ``pos``, or a
``pos`` we don't recognise) is answered as a full *initial* sync — every in-window
room gets ``initial: true`` with its full ``required_state`` and a fresh timeline
slice. A follow-up carrying a valid ``pos`` is a *delta*: only rooms with new
activity (new timeline events or changed state) are returned, newly-in-window
rooms arrive as ``initial: true``, and unchanged rooms are omitted entirely.

Multi-worker caveat
-------------------
This connection cache lives in **process memory** (one dict per
:class:`SlidingSyncService`, i.e. one per worker). With a single process — the
SQLite/desktop default, and the target scale here — that is exact. Behind
multiple workers a client whose request lands on a *different* worker than minted
its ``pos`` will not find that ``pos`` and will transparently re-initialise (a
full sync, then deltas again). That is correct, just less efficient; a
sticky-session load balancer (route a given access token to one worker) avoids
it. Persisting the connection state to the database would remove the caveat but
is deliberately out of scope at family scale — see the task notes.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from neuron_server.errors import MatrixError
from neuron_server.storage import e2ee as e2ee_store
from neuron_server.storage import rooms as store
from neuron_server.storage import userdata as userdata_store
from neuron_server.storage.database import Database
from neuron_server.storage.receipts import get_room_receipts
from neuron_server.sync.notifier import Notifier
from neuron_server.typing_state import TypingHandler

_DEFAULT_TIMELINE_LIMIT = 10
_MAX_TIMELINE_LIMIT = 100
_TO_DEVICE_LIMIT = 100
_MAX_TIMEOUT_MS = 60_000
_MAX_HEROES = 5


@dataclass
class _Snapshot:
    """What a connection had sent as of a given ``pos`` — the delta baseline."""

    pos: str
    sent_rooms: set[str] = field(default_factory=set)
    # room_id -> highest timeline stream_ordering already delivered for that room.
    room_positions: dict[str, int] = field(default_factory=dict)
    to_device_pos: int = 0
    device_list_pos: int = 0
    receipts_pos: int = 0
    typing_pos: int = 0
    # -1 so an initial sync includes pre-stream (stream_id 0) account-data rows.
    account_data_pos: int = -1


@dataclass
class _Connection:
    epoch: str
    counter: int = 0
    # pos -> snapshot. Only the most recent couple are retained (see _remember).
    snapshots: dict[str, _Snapshot] = field(default_factory=dict)


@dataclass
class _Candidate:
    """A room in the running for a list window, with its sort/filter attributes."""

    room_id: str
    membership: str
    bump_stamp: int
    is_dm: bool
    is_encrypted: bool


@dataclass
class _RoomConfig:
    required_state: list[list[str]]
    timeline_limit: int

    def merge(self, required_state: list[list[str]], timeline_limit: int) -> None:
        for entry in required_state:
            if entry not in self.required_state:
                self.required_state.append(entry)
        self.timeline_limit = max(self.timeline_limit, timeline_limit)


class SlidingSyncService:
    """Produces MSC4186 sliding-sync responses (one instance per app/worker)."""

    def __init__(
        self, db: Database, notifier: Notifier, typing: TypingHandler | None = None
    ) -> None:
        self._db = db
        self._notifier = notifier
        self._typing = typing
        # Per-process connection cache — see the module docstring's multi-worker note.
        self._connections: dict[tuple[str, str, str], _Connection] = {}

    async def sync(
        self,
        user_id: str,
        device_id: str,
        *,
        pos: str | None,
        timeout_ms: int,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        conn_id = str(body.get("conn_id") or "")
        key = (user_id, device_id, conn_id)

        response, changed, initial = await self._build(user_id, device_id, key, pos, body)
        # Long-poll only for a delta that produced nothing: an initial sync always
        # returns immediately (the client needs its first window straight away).
        if not initial and not changed and timeout_ms > 0:
            await self._notifier.wait(min(timeout_ms, _MAX_TIMEOUT_MS) / 1000.0)
            response, _, _ = await self._build(user_id, device_id, key, pos, body)

        txn_id = body.get("txn_id")
        if isinstance(txn_id, str):
            response["txn_id"] = txn_id
        return response

    async def _build(
        self,
        user_id: str,
        device_id: str,
        key: tuple[str, str, str],
        pos: str | None,
        body: dict[str, Any],
    ) -> tuple[dict[str, Any], bool, bool]:
        conn = self._connections.get(key)
        baseline: _Snapshot | None = None
        if pos and conn is not None and pos in conn.snapshots:
            baseline = conn.snapshots[pos]
        initial = baseline is None
        if initial:
            # Unknown/absent/stale pos -> start a fresh connection epoch and rebuild.
            conn = _Connection(epoch=secrets.token_hex(8))
            self._connections[key] = conn
            baseline = _Snapshot(pos="")
        assert conn is not None and baseline is not None

        changed = False

        # --- candidate rooms, sorted by recency (most-recent first) ----------
        memberships = await store.get_user_memberships(self._db, user_id)
        dm_rooms = await self._dm_rooms(user_id)
        candidates: list[_Candidate] = []
        for room_id, membership in memberships:
            if membership not in ("join", "invite"):
                continue
            candidates.append(
                _Candidate(
                    room_id=room_id,
                    membership=membership,
                    bump_stamp=await self._bump_stamp(room_id),
                    is_dm=room_id in dm_rooms,
                    is_encrypted=(
                        await store.get_state_event(
                            self._db, room_id, "m.room.encryption", ""
                        )
                        is not None
                    ),
                )
            )
        candidates.sort(key=lambda c: (-c.bump_stamp, c.room_id))
        by_id = {c.room_id: c for c in candidates}

        # --- lists: filter, count, window ------------------------------------
        lists_response: dict[str, Any] = {}
        selected: dict[str, _RoomConfig] = {}
        for name, spec in (body.get("lists") or {}).items():
            if not isinstance(spec, dict):
                continue
            filters = spec.get("filters") or {}
            filtered = [c for c in candidates if _passes_filters(filters, c)]
            lists_response[name] = {"count": len(filtered)}
            req_state = _as_pairs(spec.get("required_state"))
            timeline_limit = _clamp_limit(spec.get("timeline_limit"))
            for index in _range_indices(spec.get("ranges") or [], len(filtered)):
                room_id = filtered[index].room_id
                selected.setdefault(
                    room_id, _RoomConfig([], timeline_limit)
                ).merge(req_state, timeline_limit)

        # --- room_subscriptions: always eligible, bypassing list windows -----
        for room_id, spec in (body.get("room_subscriptions") or {}).items():
            if room_id not in by_id or not isinstance(spec, dict):
                continue  # can only subscribe to a room the user is a member of
            req_state = _as_pairs(spec.get("required_state"))
            timeline_limit = _clamp_limit(spec.get("timeline_limit"))
            selected.setdefault(
                room_id, _RoomConfig([], timeline_limit)
            ).merge(req_state, timeline_limit)

        # --- build each selected room ----------------------------------------
        rooms_response: dict[str, Any] = {}
        new_positions = dict(baseline.room_positions)
        new_sent = set(baseline.sent_rooms)
        for room_id, config in selected.items():
            candidate = by_id[room_id]
            room_initial = room_id not in baseline.sent_rooms
            room_obj, last_pos, has_content = await self._build_room(
                user_id, candidate, config, room_initial, baseline.room_positions.get(room_id, 0)
            )
            if room_initial or has_content:
                rooms_response[room_id] = room_obj
                new_sent.add(room_id)
                new_positions[room_id] = last_pos
                if not room_initial:
                    changed = True

        # --- extensions ------------------------------------------------------
        extensions, ext_changed, new_ext = await self._extensions(
            user_id, device_id, body.get("extensions") or {}, baseline, initial, selected
        )
        changed = changed or ext_changed

        # --- advance the connection pos when we delivered anything -----------
        produce = initial or changed
        if produce:
            conn.counter += 1
            new_pos = f"{conn.epoch}_{conn.counter}"
            snapshot = _Snapshot(
                pos=new_pos,
                sent_rooms=new_sent,
                room_positions=new_positions,
                to_device_pos=new_ext["to_device"],
                device_list_pos=new_ext["device_list"],
                receipts_pos=new_ext["receipts"],
                typing_pos=new_ext["typing"],
                account_data_pos=new_ext["account_data"],
            )
            self._remember(conn, snapshot)
        else:
            new_pos = baseline.pos

        response = {
            "pos": new_pos,
            "lists": lists_response,
            "rooms": rooms_response,
            "extensions": extensions,
        }
        return response, changed, initial

    @staticmethod
    def _remember(conn: _Connection, snapshot: _Snapshot) -> None:
        """Store a snapshot, keeping only the two most recent (current + a retry)."""
        conn.snapshots[snapshot.pos] = snapshot
        while len(conn.snapshots) > 2:
            oldest = min(conn.snapshots, key=lambda p: int(p.rsplit("_", 1)[-1]))
            del conn.snapshots[oldest]

    # --- per-room ----------------------------------------------------------

    async def _build_room(
        self,
        user_id: str,
        candidate: _Candidate,
        config: _RoomConfig,
        room_initial: bool,
        since_ordering: int,
    ) -> tuple[dict[str, Any], int, bool]:
        room_id = candidate.room_id
        state_events = await store.get_current_state(self._db, room_id)
        state_map = {(e.type, e.state_key or ""): e for e in state_events}

        # Timeline: a fresh recent slice on initial, only new events on a delta.
        if room_initial:
            timeline = await store.get_recent_events(self._db, room_id, config.timeline_limit)
            limited = len(timeline) >= config.timeline_limit
            prev_batch = (timeline[0].stream_ordering - 1) if timeline else 0
            has_content = True
        else:
            fetched = await store.get_messages(
                self._db,
                room_id,
                from_ordering=since_ordering,
                direction="f",
                limit=config.timeline_limit + 1,
            )
            limited = len(fetched) > config.timeline_limit
            timeline = fetched[: config.timeline_limit]
            prev_batch = since_ordering
            has_content = bool(timeline)

        last_pos = (
            timeline[-1].stream_ordering if timeline else max(since_ordering, candidate.bump_stamp)
        )
        timeline_senders = {e.sender for e in timeline}
        # On a delta, required_state carries only state that changed since the
        # baseline (stream_ordering > since); lazy-loaded members of new timeline
        # senders are always included regardless (see _required_state).
        min_stream = -1 if room_initial else since_ordering
        required_state = _required_state(
            config.required_state, state_events, timeline_senders, user_id, min_stream
        )
        if required_state and not room_initial:
            has_content = True

        joined_members = [
            key[1]
            for key, ev in state_map.items()
            if key[0] == "m.room.member" and ev.content.get("membership") == "join"
        ]
        invited = sum(
            1
            for key, ev in state_map.items()
            if key[0] == "m.room.member" and ev.content.get("membership") == "invite"
        )
        name = _state_content(state_map, "m.room.name").get("name")
        avatar = _state_content(state_map, "m.room.avatar").get("url")
        notifications, highlights = await self._unread(room_id, user_id)

        room_obj: dict[str, Any] = {
            "initial": room_initial,
            "required_state": required_state,
            "timeline": [e.client_dict() for e in timeline],
            "prev_batch": str(prev_batch),
            "limited": limited,
            "joined_count": len(joined_members),
            "invited_count": invited,
            "notification_count": notifications,
            "highlight_count": highlights,
            "bump_stamp": candidate.bump_stamp,
            "is_dm": candidate.is_dm,
        }
        if name is not None:
            room_obj["name"] = name
        if avatar is not None:
            room_obj["avatar"] = avatar
        if name is None:
            # No m.room.name: give the client heroes so it can derive a name, like
            # the classic sync's room summary would.
            room_obj["heroes"] = _heroes(state_map, joined_members, user_id)
        return room_obj, last_pos, has_content

    async def _bump_stamp(self, room_id: str) -> int:
        """Recency key: the stream ordering of the room's latest event.

        The forward extremity is the tip of our linear DAG (the most recent
        message or state event), so its stream ordering is a monotonic, per-room
        "last activity" position — exactly what MSC4186's ``bump_stamp`` wants.
        """
        extremity = await store.get_forward_extremity(self._db, room_id)
        return extremity.stream_ordering if extremity is not None else 0

    async def _unread(self, room_id: str, user_id: str) -> tuple[int, int]:
        from neuron_server.storage import receipts as receipts_store

        terms = await self._highlight_terms(user_id)
        return await receipts_store.get_unread_counts(self._db, room_id, user_id, terms)

    async def _highlight_terms(self, user_id: str) -> list[str]:
        terms = [user_id.split(":", 1)[0].lstrip("@")]
        profile = await userdata_store.get_profile(self._db, user_id)
        displayname = profile.get("displayname")
        if displayname:
            terms.append(str(displayname))
        return terms

    async def _dm_rooms(self, user_id: str) -> set[str]:
        """The set of room ids the user has flagged as DMs (``m.direct``)."""
        content = await userdata_store.get_account_data(self._db, user_id, "", "m.direct")
        if not content:
            return set()
        rooms: set[str] = set()
        for room_ids in content.values():
            if isinstance(room_ids, list):
                rooms.update(str(r) for r in room_ids)
        return rooms

    # --- extensions --------------------------------------------------------

    async def _extensions(
        self,
        user_id: str,
        device_id: str,
        spec: dict[str, Any],
        baseline: _Snapshot,
        initial: bool,
        selected: dict[str, _RoomConfig],
    ) -> tuple[dict[str, Any], bool, dict[str, int]]:
        extensions: dict[str, Any] = {}
        changed = False
        # Carry the baseline positions forward by default; each enabled extension
        # advances its own to the current stream position.
        new_pos = {
            "to_device": baseline.to_device_pos,
            "device_list": baseline.device_list_pos,
            "receipts": baseline.receipts_pos,
            "typing": baseline.typing_pos,
            "account_data": baseline.account_data_pos,
        }

        to_device = spec.get("to_device")
        if isinstance(to_device, dict) and to_device.get("enabled"):
            since = baseline.to_device_pos
            raw_since = to_device.get("since")
            if raw_since is not None:
                try:
                    since = int(raw_since)
                except (TypeError, ValueError):
                    since = baseline.to_device_pos
            events, next_pos = await self._drain_to_device(user_id, device_id, since)
            extensions["to_device"] = {"events": events, "next_batch": str(next_pos)}
            new_pos["to_device"] = next_pos
            changed = changed or bool(events)

        e2ee = spec.get("e2ee")
        if isinstance(e2ee, dict) and e2ee.get("enabled"):
            device_pos = await self._db.get_stream_position("device_lists")
            if initial:
                device_changed: list[str] = []
            else:
                changed_users = await e2ee_store.get_device_list_changes_after(
                    self._db, baseline.device_list_pos
                )
                sharing = set(await store.get_users_sharing_room(self._db, user_id))
                device_changed = [u for u in changed_users if u in sharing]
            otk = await e2ee_store.count_one_time_keys(self._db, user_id, device_id)
            extensions["e2ee"] = {
                "device_lists": {"changed": device_changed, "left": []},
                "device_one_time_keys_count": otk,
                "device_unused_fallback_key_types": await self._unused_fallback_types(
                    user_id, device_id
                ),
            }
            new_pos["device_list"] = device_pos
            changed = changed or bool(device_changed)

        account_data = spec.get("account_data")
        if isinstance(account_data, dict) and account_data.get("enabled"):
            entries = await userdata_store.get_account_data_changes(
                self._db, user_id, -1 if initial else baseline.account_data_pos
            )
            global_data: list[dict[str, Any]] = []
            room_data: dict[str, list[dict[str, Any]]] = {}
            for entry in entries:
                event = {"type": entry.data_type, "content": entry.content}
                if entry.room_id:
                    room_data.setdefault(entry.room_id, []).append(event)
                else:
                    global_data.append(event)
            extensions["account_data"] = {"global": global_data, "rooms": room_data}
            new_pos["account_data"] = await self._db.get_stream_position("account_data")
            changed = changed or (not initial and bool(global_data or room_data))

        receipts = spec.get("receipts")
        if isinstance(receipts, dict) and receipts.get("enabled"):
            target = _rooms_filter(receipts.get("rooms"), selected)
            receipts_out: dict[str, Any] = {}
            for room_id in target:
                content, room_changed = await self._room_receipts(
                    room_id, user_id, baseline.receipts_pos, initial
                )
                if content is not None:
                    receipts_out[room_id] = content
                changed = changed or (room_changed and not initial)
            extensions["receipts"] = {"rooms": receipts_out}
            new_pos["receipts"] = await self._db.get_stream_position("receipts")

        typing = spec.get("typing")
        if isinstance(typing, dict) and typing.get("enabled") and self._typing is not None:
            target = _rooms_filter(typing.get("rooms"), selected)
            typing_out: dict[str, Any] = {}
            for room_id in target:
                users = await self._typing.typing_users(room_id)
                if users:
                    typing_out[room_id] = {"user_ids": users}
            serial = await self._typing.serial()
            extensions["typing"] = {"rooms": typing_out}
            new_pos["typing"] = serial
            changed = changed or (not initial and serial > baseline.typing_pos)

        return extensions, changed, new_pos

    async def _drain_to_device(
        self, user_id: str, device_id: str, since: int
    ) -> tuple[list[dict[str, Any]], int]:
        # Messages up to the acknowledged position have been received — clear them
        # (identical to the classic /sync to-device handling).
        if since > 0:
            await e2ee_store.delete_to_device_up_to(self._db, user_id, device_id, since)
        pending = await e2ee_store.get_to_device(
            self._db, user_id, device_id, since, _TO_DEVICE_LIMIT
        )
        events = [message for _, message in pending]
        next_pos = pending[-1][0] if pending else since
        return events, next_pos

    async def _room_receipts(
        self, room_id: str, user_id: str, since: int, initial: bool
    ) -> tuple[dict[str, Any] | None, bool]:
        """The room's ``m.receipt`` content + whether it changed since ``since``.

        Mirrors the classic sync's private-receipt rule: ``m.read.private`` is only
        ever shown to its own owner.
        """
        receipts = [
            r
            for r in await get_room_receipts(self._db, room_id)
            if r.receipt_type != "m.read.private" or r.user_id == user_id
        ]
        if not receipts:
            return None, False
        content: dict[str, Any] = {}
        for receipt in receipts:
            by_type = content.setdefault(receipt.event_id, {}).setdefault(receipt.receipt_type, {})
            by_type[receipt.user_id] = {"ts": receipt.ts}
        changed = initial or any(r.stream_id > since for r in receipts)
        return content, changed

    async def _unused_fallback_types(self, user_id: str, device_id: str) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT DISTINCT algorithm FROM fallback_keys"
            " WHERE user_id = ? AND device_id = ? AND used = 0",
            (user_id, device_id),
        )
        return [str(row[0]) for row in rows]


# --- module helpers --------------------------------------------------------


def _as_pairs(value: Any) -> list[list[str]]:
    """Normalise a ``required_state`` spec into a list of ``[type, state_key]``."""
    if not isinstance(value, list):
        return []
    pairs: list[list[str]] = []
    for entry in value:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            pairs.append([str(entry[0]), str(entry[1])])
    return pairs


def _clamp_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_TIMELINE_LIMIT
    return max(0, min(limit, _MAX_TIMELINE_LIMIT))


def _range_indices(ranges: Any, count: int) -> list[int]:
    """The room indices covered by a list's inclusive ``[start, end]`` ranges."""
    indices: list[int] = []
    seen: set[int] = set()
    if not isinstance(ranges, list):
        return indices
    for entry in ranges:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            continue
        try:
            start = max(0, int(entry[0]))
            end = min(count - 1, int(entry[1]))
        except (TypeError, ValueError):
            continue
        for index in range(start, end + 1):
            if index not in seen:
                seen.add(index)
                indices.append(index)
    return indices


def _passes_filters(filters: dict[str, Any], candidate: _Candidate) -> bool:
    if "is_dm" in filters and bool(filters["is_dm"]) != candidate.is_dm:
        return False
    if "is_encrypted" in filters and bool(filters["is_encrypted"]) != candidate.is_encrypted:
        return False
    return True


def _rooms_filter(
    rooms: Any, selected: dict[str, _RoomConfig]
) -> list[str]:
    """Rooms an extension should report on: an explicit list, else all in-window."""
    if isinstance(rooms, list):
        return [str(r) for r in rooms]
    return list(selected)


def _state_content(state_map: dict[tuple[str, str], Any], etype: str) -> dict[str, Any]:
    event = state_map.get((etype, ""))
    return event.content if event is not None else {}


def _required_state(
    patterns: list[list[str]],
    state_events: list[Any],
    timeline_senders: set[str],
    user_id: str,
    min_stream: int,
) -> list[dict[str, Any]]:
    """Resolve a room's ``required_state`` request against its current state.

    Sentinels: ``["*","*"]`` (all state), ``[type,"*"]`` (all of a type),
    ``["m.room.member","$LAZY"]`` (only members referenced by the timeline plus the
    syncing user), ``[type,"$ME"]`` (the syncing user's own state key). ``min_stream``
    restricts pattern matches to state that changed after a delta baseline (pass
    ``-1`` to include everything on an initial sync); lazily-loaded members are
    always included so a new message's sender profile is never withheld.
    """
    wanted_all = False
    type_wildcards: set[str] = set()
    exact: set[tuple[str, str]] = set()
    me_types: set[str] = set()
    lazy = False
    for etype, state_key in patterns:
        if etype == "*" and state_key == "*":
            wanted_all = True
        elif state_key == "*":
            type_wildcards.add(etype)
        elif etype == "m.room.member" and state_key == "$LAZY":
            lazy = True
        elif state_key == "$ME":
            me_types.add(etype)
        else:
            exact.add((etype, state_key))

    lazy_members = (timeline_senders | {user_id}) if lazy else set()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for event in state_events:
        state_key = event.state_key or ""
        key = (event.type, state_key)
        lazy_hit = lazy and event.type == "m.room.member" and state_key in lazy_members
        pattern_hit = (
            wanted_all
            or event.type in type_wildcards
            or key in exact
            or (event.type in me_types and state_key == user_id)
        )
        if lazy_hit:
            result[key] = event.client_dict()  # always, even on a delta
        elif pattern_hit and event.stream_ordering > min_stream:
            result[key] = event.client_dict()
    return list(result.values())


def _heroes(
    state_map: dict[tuple[str, str], Any], joined_members: list[str], user_id: str
) -> list[dict[str, Any]]:
    """Up to five other members, for a client to derive a name for an unnamed room."""
    heroes: list[dict[str, Any]] = []
    for member in joined_members:
        if member == user_id:
            continue
        entry: dict[str, Any] = {"user_id": member}
        event = state_map.get(("m.room.member", member))
        if event is not None:
            if event.content.get("displayname"):
                entry["displayname"] = event.content["displayname"]
            if event.content.get("avatar_url"):
                entry["avatar_url"] = event.content["avatar_url"]
        heroes.append(entry)
        if len(heroes) >= _MAX_HEROES:
            break
    return heroes


def parse_pos(pos: str | None) -> str | None:
    """Validate a ``pos`` token's shape; unknown-but-well-formed tokens rebuild.

    A malformed token is rejected up front (M_INVALID_PARAM) rather than silently
    treated as initial, so a client bug surfaces instead of looking like a reset.
    """
    if pos is None:
        return None
    if "_" not in pos or not pos.split("_")[-1].isdigit():
        raise MatrixError(400, "M_INVALID_PARAM", "Invalid pos token")
    return pos
