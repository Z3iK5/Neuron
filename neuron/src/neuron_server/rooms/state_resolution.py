# SPDX-License-Identifier: Apache-2.0
"""State resolution version 2 (HS-7 step 6c).

Implements the Matrix room-v2+ state-resolution algorithm: given several
candidate state maps (e.g. from forks of the room DAG), produce a single resolved
state. The shape of the algorithm follows the spec ("State Resolution"):

1. split state into the **unconflicted** map and the **conflicted** set;
2. compute the **auth difference** and the full conflicted set;
3. order the conflicted **power events** by *reverse topological power ordering*
   and apply the auth rules iteratively;
4. order the remaining conflicted events by **mainline ordering** (relative to the
   resolved ``m.room.power_levels``) and apply the auth rules iteratively;
5. overlay the unconflicted map (which always wins).

This operates purely on an in-memory event map, so it is independent of storage
and directly unit-testable.

⚠️ Status: this is **spec-guided and scenario-tested**, but it has not yet been
validated against the official Complement / sytest state-resolution vectors, so
byte-exact conformance in adversarial tie-break cases is not guaranteed. It is not
yet wired into the live ingestion path (single-resident joins don't fork); it is
the foundation for conflict handling and is exercised by its unit tests.
"""

from __future__ import annotations

import heapq

from neuron_server.errors import MatrixError
from neuron_server.rooms import authrules
from neuron_server.rooms.events import Event

StateKey = tuple[str, str]
StateMap = dict[StateKey, str]

_CREATE = ("m.room.create", "")
_POWER_LEVELS = ("m.room.power_levels", "")


def _key_of(event: Event) -> StateKey:
    return (event.type, event.state_key or "")


def separate(state_maps: list[StateMap]) -> tuple[StateMap, dict[StateKey, set[str]]]:
    """Split into the unconflicted map and the conflicted set.

    A key is *unconflicted* if every state map that contains it agrees on the value
    (maps lacking the key are ignored). Otherwise it is conflicted.
    """
    unconflicted: StateMap = {}
    conflicted: dict[StateKey, set[str]] = {}
    all_keys: set[StateKey] = set()
    for state_map in state_maps:
        all_keys |= set(state_map.keys())
    for key in all_keys:
        values = {state_map[key] for state_map in state_maps if key in state_map}
        if len(values) == 1:
            unconflicted[key] = next(iter(values))
        else:
            conflicted[key] = values
    return unconflicted, conflicted


def _auth_chain(event_id: str, event_map: dict[str, Event]) -> set[str]:
    """The transitive closure of an event's ``auth_events`` (excluding itself)."""
    seen: set[str] = set()
    start = event_map.get(event_id)
    stack = list(start.auth_events) if start else []
    while stack:
        current = stack.pop()
        if current in seen or current not in event_map:
            continue
        seen.add(current)
        stack.extend(event_map[current].auth_events)
    return seen


def auth_difference(state_maps: list[StateMap], event_map: dict[str, Event]) -> set[str]:
    """Events in some — but not all — of the state sets' auth chains."""
    chains: list[set[str]] = []
    for state_map in state_maps:
        chain: set[str] = set()
        for event_id in state_map.values():
            chain.add(event_id)
            chain |= _auth_chain(event_id, event_map)
        chains.append(chain)
    if not chains:
        return set()
    union = set().union(*chains)
    intersection = set(chains[0]).intersection(*chains[1:])
    return union - intersection


def is_power_event(event: Event) -> bool:
    """Whether ``event`` can remove another user's privileges (a "control" event)."""
    if event.type == "m.room.power_levels" and (event.state_key or "") == "":
        return True
    if event.type == "m.room.join_rules" and (event.state_key or "") == "":
        return True
    if event.type == "m.room.member":
        membership = (event.content or {}).get("membership")
        if membership in ("leave", "ban") and event.sender != event.state_key:
            return True
    return False


def _sender_power_level(event: Event, event_map: dict[str, Event]) -> int:
    """The sender's power level as implied by the event's own auth events."""
    power_levels: Event | None = None
    create: Event | None = None
    for auth_id in event.auth_events:
        auth_event = event_map.get(auth_id)
        if auth_event is None:
            continue
        if _key_of(auth_event) == _POWER_LEVELS:
            power_levels = auth_event
        elif _key_of(auth_event) == _CREATE:
            create = auth_event
    if power_levels is None:
        # No power levels yet: the room creator (the create event's sender) is 100.
        if create is not None and event.sender == create.sender:
            return 100
        return 0
    users = power_levels.content.get("users", {})
    if event.sender in users:
        return int(users[event.sender])
    return int(power_levels.content.get("users_default", 0))


def reverse_topological_power_sort(
    event_ids: set[str], event_map: dict[str, Event]
) -> list[str]:
    """Kahn topological sort by auth dependency, ties broken by
    ``(sender power level, origin_server_ts, event_id)`` (ascending)."""
    parents = {
        eid: {a for a in event_map[eid].auth_events if a in event_ids} for eid in event_ids
    }
    children: dict[str, set[str]] = {eid: set() for eid in event_ids}
    for eid, deps in parents.items():
        for dep in deps:
            children[dep].add(eid)

    sort_key = {
        eid: (
            _sender_power_level(event_map[eid], event_map),
            event_map[eid].origin_server_ts,
            eid,
        )
        for eid in event_ids
    }
    indegree = {eid: len(parents[eid]) for eid in event_ids}
    heap: list[tuple[tuple[int, int, str], str]] = [
        (sort_key[eid], eid) for eid in event_ids if indegree[eid] == 0
    ]
    heapq.heapify(heap)

    order: list[str] = []
    while heap:
        _, eid = heapq.heappop(heap)
        order.append(eid)
        for child in children[eid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(heap, (sort_key[child], child))
    return order


def _mainline(power_levels_id: str, event_map: dict[str, Event]) -> dict[str, int]:
    """Positions along the mainline of a power-levels event (root = 0, head highest)."""
    line: list[str] = []
    current: str | None = power_levels_id
    while current is not None:
        line.append(current)
        event = event_map.get(current)
        current = None
        if event is not None:
            for auth_id in event.auth_events:
                auth_event = event_map.get(auth_id)
                if auth_event is not None and _key_of(auth_event) == _POWER_LEVELS:
                    current = auth_id
                    break
    return {eid: index for index, eid in enumerate(reversed(line))}


def _mainline_position(
    event_id: str, mainline: dict[str, int], event_map: dict[str, Event]
) -> int:
    current: str | None = event_id
    while current is not None:
        if current in mainline:
            return mainline[current]
        event = event_map.get(current)
        current = None
        if event is not None:
            for auth_id in event.auth_events:
                auth_event = event_map.get(auth_id)
                if auth_event is not None and _key_of(auth_event) == _POWER_LEVELS:
                    current = auth_id
                    break
    return 0


def _mainline_sort(
    event_ids: list[str], power_levels_id: str | None, event_map: dict[str, Event]
) -> list[str]:
    if power_levels_id is None:
        return sorted(event_ids, key=lambda eid: (event_map[eid].origin_server_ts, eid))
    mainline = _mainline(power_levels_id, event_map)
    return sorted(
        event_ids,
        key=lambda eid: (
            _mainline_position(eid, mainline, event_map),
            event_map[eid].origin_server_ts,
            eid,
        ),
    )


def _iterative_auth_checks(
    order: list[str], base_state: StateMap, event_map: dict[str, Event]
) -> StateMap:
    """Apply auth rules to ``order`` in turn, keeping each event that is allowed."""
    resolved: StateMap = dict(base_state)
    for event_id in order:
        event = event_map.get(event_id)
        if event is None:
            continue
        auth_state: dict[StateKey, Event] = {
            key: event_map[value] for key, value in resolved.items() if value in event_map
        }
        try:
            authrules.authorize(event, auth_state)
        except MatrixError:
            continue
        resolved[_key_of(event)] = event_id
    return resolved


def resolve(state_maps: list[StateMap], event_map: dict[str, Event]) -> StateMap:
    """Resolve ``state_maps`` into a single state map (state resolution v2)."""
    if len(state_maps) <= 1:
        return dict(state_maps[0]) if state_maps else {}

    unconflicted, conflicted = separate(state_maps)
    if not conflicted:
        return unconflicted

    full_conflicted: set[str] = set()
    for values in conflicted.values():
        full_conflicted |= values
    full_conflicted |= auth_difference(state_maps, event_map)
    full_conflicted = {eid for eid in full_conflicted if eid in event_map}

    control_events = {eid for eid in full_conflicted if is_power_event(event_map[eid])}
    sorted_control = reverse_topological_power_sort(control_events, event_map)
    resolved = _iterative_auth_checks(sorted_control, unconflicted, event_map)

    remaining = [eid for eid in full_conflicted if eid not in control_events]
    resolved_power_levels = resolved.get(_POWER_LEVELS)
    sorted_remaining = _mainline_sort(remaining, resolved_power_levels, event_map)
    resolved = _iterative_auth_checks(sorted_remaining, resolved, event_map)

    resolved.update(unconflicted)
    return resolved


__all__ = [
    "StateKey",
    "StateMap",
    "auth_difference",
    "is_power_event",
    "resolve",
    "reverse_topological_power_sort",
    "separate",
]
