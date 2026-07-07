# SPDX-License-Identifier: Apache-2.0
"""Evaluate a user's merged push ruleset against a single event.

Walks the rules in the spec's priority order (override, content, room, sender,
underride) and returns the FIRST matching rule's decision: whether to notify and
which tweaks (sound / highlight) apply. Only the condition kinds the shipped
:mod:`neuron_server.pushrules` defaults use — plus custom ``event_match`` rules —
are implemented; anything unknown is treated as a non-match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from neuron_server.pushrules import KINDS

# The order push rules are evaluated in (highest priority first).
_EVAL_ORDER = ("override", "content", "room", "sender", "underride")


@dataclass(frozen=True)
class Decision:
    notify: bool
    highlight: bool = False
    sound: str | None = None
    actions: list[Any] | None = None


@dataclass(frozen=True)
class RoomContext:
    """The room-level facts the conditions need, resolved once per event."""

    member_count: int
    sender_power_level: int
    notification_levels: dict[str, int]


def _dotted(event: dict[str, Any], key: str) -> Any:
    value: Any = event
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _glob_to_regex(pattern: str) -> str:
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _match_glob(value: Any, pattern: str) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(_glob_to_regex(pattern), value, re.IGNORECASE) is not None


def _match_word(text: Any, pattern: str) -> bool:
    """Whether ``pattern`` (glob) appears as a word in ``text`` — the spec's
    ``content.body`` / content-rule matching (word boundaries, case-insensitive)."""
    if not isinstance(text, str) or not pattern:
        return False
    regex = r"(^|\W)(?:" + _glob_to_regex(pattern) + r")($|\W)"
    return re.search(regex, text, re.IGNORECASE) is not None


def _member_count_matches(spec: str, count: int) -> bool:
    spec = spec.strip()
    for op in ("<=", ">=", "==", "<", ">"):
        if spec.startswith(op):
            rest = spec[len(op):].strip()
            if not rest.isdigit():
                return False
            n = int(rest)
            if op == "<=":
                return count <= n
            if op == ">=":
                return count >= n
            if op == "<":
                return count < n
            if op == ">":
                return count > n
            return count == n
    return spec.isdigit() and count == int(spec)


def _condition_matches(
    condition: dict[str, Any],
    event: dict[str, Any],
    *,
    display_name: str | None,
    ctx: RoomContext,
) -> bool:
    kind = condition.get("kind")
    if kind == "event_match":
        key = str(condition.get("key", ""))
        pattern = condition.get("pattern")
        if not isinstance(pattern, str):
            return False
        value = _dotted(event, key)
        if key == "content.body":
            return _match_word(value, pattern)
        return _match_glob(value, pattern)
    if kind == "contains_display_name":
        if not display_name:
            return False
        return _match_word(_dotted(event, "content.body"), display_name)
    if kind == "room_member_count":
        spec = condition.get("is")
        return isinstance(spec, str) and _member_count_matches(spec, ctx.member_count)
    if kind == "sender_notification_permission":
        key = str(condition.get("key", "room"))
        required = int(ctx.notification_levels.get(key, 50))
        return ctx.sender_power_level >= required
    # Unknown condition kind: fail closed (no match).
    return False


def _decision_from_actions(actions: list[Any]) -> Decision:
    notify = False
    highlight = False
    sound: str | None = None
    for action in actions:
        if action == "notify":
            notify = True
        elif action == "dont_notify":
            notify = False
        elif isinstance(action, dict) and action.get("set_tweak") == "sound":
            sound = str(action.get("value", "default"))
        elif isinstance(action, dict) and action.get("set_tweak") == "highlight":
            highlight = action.get("value", True) is not False
    return Decision(notify=notify, highlight=highlight, sound=sound, actions=actions)


def _rule_matches(
    rule: dict[str, Any],
    kind: str,
    event: dict[str, Any],
    *,
    display_name: str | None,
    ctx: RoomContext,
) -> bool:
    if kind == "content":
        pattern = rule.get("pattern")
        return isinstance(pattern, str) and _match_word(
            _dotted(event, "content.body"), pattern
        )
    if kind in ("room", "sender"):
        # A room/sender rule's rule_id is the room_id / sender it targets.
        target_key = "room_id" if kind == "room" else "sender"
        return event.get(target_key) == rule.get("rule_id")
    conditions = rule.get("conditions") or []
    return all(
        _condition_matches(c, event, display_name=display_name, ctx=ctx)
        for c in conditions
    )


def evaluate(
    ruleset: dict[str, list[dict[str, Any]]],
    event: dict[str, Any],
    *,
    display_name: str | None,
    ctx: RoomContext,
) -> Decision:
    """Return the decision from the first enabled matching rule (``notify=False``
    if nothing matches)."""
    assert set(_EVAL_ORDER) == set(KINDS)  # priority order must cover every kind
    for kind in _EVAL_ORDER:
        for rule in ruleset.get(kind, []):
            if not rule.get("enabled", True):
                continue
            if _rule_matches(rule, kind, event, display_name=display_name, ctx=ctx):
                return _decision_from_actions(rule.get("actions") or [])
    return Decision(notify=False)
