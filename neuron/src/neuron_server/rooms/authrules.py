# SPDX-License-Identifier: Apache-2.0
"""Event authorization rules (room version 11).

Given an event and the room's current state, decide whether the event is allowed.
This implements the Matrix spec's authorization rules for the event types the
single-server MVP supports (create, membership transitions, power levels, and the
generic power-level check for everything else).

Follows the spec's "Authorization rules" section, scoped to a single server (no
state-resolution — the current state *is* the auth context).

Not yet covered (honest scope): knock / restricted (membership) join rules,
third-party invites, and the full fine-grained power-levels delta rules (we use
the well-known "you may not set or change a level above your own" approximation).
"""

from __future__ import annotations

from typing import Any

from neuron_server.errors import MatrixError
from neuron_server.rooms.events import Event

# State keys we read from the auth context.
_CREATE = ("m.room.create", "")
_POWER_LEVELS = ("m.room.power_levels", "")
_JOIN_RULES = ("m.room.join_rules", "")

AuthState = dict[tuple[str, str], Event]

# Default power-level values when a field is absent from m.room.power_levels.
_DEFAULTS = {
    "ban": 50,
    "kick": 50,
    "redact": 50,
    "invite": 0,
    "events_default": 0,
    "state_default": 50,
    "users_default": 0,
}


def select_auth_event_ids(
    etype: str, state_key: str | None, sender: str, content: dict[str, Any], state: AuthState
) -> list[str]:
    """The spec's "auth events selection": the state events that authorise a new
    event of this type, returned as a de-duplicated, order-preserving id list.

    ``m.room.create`` is its own auth root and selects nothing.
    """
    if etype == "m.room.create":
        return []

    ids: list[str] = []

    def add(key: tuple[str, str]) -> None:
        event = state.get(key)
        if event is not None:
            ids.append(event.event_id)

    add(("m.room.create", ""))
    add(("m.room.power_levels", ""))
    add(("m.room.member", sender))

    if etype == "m.room.member":
        if state_key is not None:
            add(("m.room.member", state_key))
        membership = content.get("membership")
        if membership in ("join", "invite"):
            add(("m.room.join_rules", ""))
        third_party = content.get("third_party_invite")
        if isinstance(third_party, dict):
            token = third_party.get("signed", {}).get("token")
            if isinstance(token, str):
                add(("m.room.third_party_invite", token))
        authorising = content.get("join_authorised_via_users_server")
        if isinstance(authorising, str):
            add(("m.room.member", authorising))

    seen: set[str] = set()
    unique: list[str] = []
    for event_id in ids:
        if event_id not in seen:
            seen.add(event_id)
            unique.append(event_id)
    return unique


def _forbidden(message: str) -> MatrixError:
    return MatrixError(403, "M_FORBIDDEN", message)


def _power_levels(state: AuthState) -> dict[str, Any] | None:
    event = state.get(_POWER_LEVELS)
    return event.content if event else None


def _creator(state: AuthState) -> str | None:
    event = state.get(_CREATE)
    return event.sender if event else None


def power_level_for(state: AuthState, user_id: str) -> int:
    """The effective power level of ``user_id`` in the room's current state."""
    pl = _power_levels(state)
    if pl is None:
        # Before an m.room.power_levels exists, the room creator has PL 100.
        return 100 if user_id == _creator(state) else 0
    users = pl.get("users", {})
    if user_id in users:
        return int(users[user_id])
    return int(pl.get("users_default", 0))


def named_level(state: AuthState, key: str) -> int:
    """The room's action level (e.g. ``kick``/``ban``/``redact``), with spec defaults."""
    pl = _power_levels(state)
    if pl is None or key not in pl:
        return _DEFAULTS[key]
    return int(pl[key])


def membership_of(state: AuthState, user_id: str) -> str | None:
    event = state.get(("m.room.member", user_id))
    return event.content.get("membership") if event else None


def _join_rule(state: AuthState) -> str:
    event = state.get(_JOIN_RULES)
    return event.content.get("join_rule", "invite") if event else "invite"


def authorize(event: Event, state: AuthState) -> None:
    """Raise :class:`MatrixError` (403 M_FORBIDDEN) if ``event`` is not allowed."""
    if event.type == "m.room.create":
        # createRoom builds this as the first event; further creates are invalid.
        if state.get(_CREATE) is not None:
            raise _forbidden("Room already has a create event")
        return

    if state.get(_CREATE) is None:
        raise _forbidden("Room does not exist")

    if event.type == "m.room.member":
        _authorize_membership(event, state)
        return

    if membership_of(state, event.sender) != "join":
        raise _forbidden("User is not in the room")

    if event.type == "m.room.power_levels":
        _authorize_power_levels(event, state)
        return

    required = _required_level(event, state)
    if power_level_for(state, event.sender) < required:
        raise _forbidden(f"Insufficient power level to send {event.type}")


def _authorize_membership(event: Event, state: AuthState) -> None:
    target = event.state_key or ""
    membership = event.content.get("membership")
    sender = event.sender
    sender_membership = membership_of(state, sender)
    target_membership = membership_of(state, target)

    if membership == "join":
        if target != sender:
            raise _forbidden("Cannot force another user to join")
        if target_membership == "ban":
            raise _forbidden("You are banned from this room")
        rule = _join_rule(state)
        if rule == "public":
            return
        if rule == "invite" and target_membership in ("invite", "join"):
            return
        raise _forbidden("You are not invited to this room")

    if membership == "invite":
        if sender_membership != "join":
            raise _forbidden("Inviter is not in the room")
        if target_membership in ("join", "ban"):
            raise _forbidden("User is already joined or banned")
        if power_level_for(state, sender) < named_level(state, "invite"):
            raise _forbidden("Insufficient power level to invite")
        return

    if membership == "leave":
        if sender == target:
            if sender_membership in ("join", "invite"):
                return
            raise _forbidden("Cannot leave the room")
        # Kicking another user.
        if sender_membership != "join":
            raise _forbidden("Kicker is not in the room")
        # Lifting a ban additionally requires the ban level (room v11 auth rules).
        if target_membership == "ban" and power_level_for(state, sender) < named_level(
            state, "ban"
        ):
            raise _forbidden("Insufficient power level to unban")
        if power_level_for(state, sender) < named_level(state, "kick"):
            raise _forbidden("Insufficient power level to kick")
        if power_level_for(state, target) >= power_level_for(state, sender):
            raise _forbidden("Cannot kick a user with an equal or higher power level")
        return

    if membership == "ban":
        if sender_membership != "join":
            raise _forbidden("Banner is not in the room")
        if power_level_for(state, sender) < named_level(state, "ban"):
            raise _forbidden("Insufficient power level to ban")
        if power_level_for(state, target) >= power_level_for(state, sender):
            raise _forbidden("Cannot ban a user with an equal or higher power level")
        return

    raise _forbidden(f"Unsupported membership: {membership!r}")


def _required_level(event: Event, state: AuthState) -> int:
    pl = _power_levels(state)
    events_map = pl.get("events", {}) if pl else {}
    if event.type in events_map:
        return int(events_map[event.type])
    if event.is_state:
        return named_level(state, "state_default")
    return named_level(state, "events_default")


def _authorize_power_levels(event: Event, state: AuthState) -> None:
    """Approximate the spec's power-levels rules: the sender may not set or change
    any level (its old or new value) above their own."""
    sender_level = power_level_for(state, event.sender)
    old = _power_levels(state) or {}
    new = event.content

    for key in ("ban", "kick", "redact", "invite", "events_default", "state_default",
                "users_default"):
        _check_level_change(key, old.get(key), new.get(key), sender_level)

    _check_mapping_changes(old.get("events", {}), new.get("events", {}), sender_level)
    _check_mapping_changes(old.get("users", {}), new.get("users", {}), sender_level)


def _check_level_change(
    name: str, old_value: Any, new_value: Any, sender_level: int
) -> None:
    if old_value == new_value:
        return
    levels = [v for v in (old_value, new_value) if isinstance(v, int)]
    if levels and sender_level < max(levels):
        raise _forbidden(f"Insufficient power level to change '{name}'")


def _check_mapping_changes(
    old_map: dict[str, Any], new_map: dict[str, Any], sender_level: int
) -> None:
    for key in set(old_map) | set(new_map):
        old_value = old_map.get(key)
        new_value = new_map.get(key)
        if old_value == new_value:
            continue
        levels = [v for v in (old_value, new_value) if isinstance(v, int)]
        if levels and sender_level < max(levels):
            raise _forbidden("Insufficient power level to change power levels")
