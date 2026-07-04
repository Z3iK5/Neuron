# SPDX-License-Identifier: Apache-2.0
"""Push-rule rulesets: the spec's predefined server-default rules and the merge
with a user's stored rules/overrides.

The Client-Server spec defines a predefined ruleset every homeserver serves
(rule ids starting with ``.``). Users can tweak a predefined rule's ``enabled``
flag and ``actions``, and add custom rules. Priority within the merged ruleset
follows the spec: ``.m.rule.master`` outranks everything, user-defined rules
outrank the remaining server defaults.
"""

from __future__ import annotations

from typing import Any

from neuron_server.storage.push import PushRuleRow

KINDS = ("override", "content", "room", "sender", "underride")

# Kinds whose rules carry a ``conditions`` list / a ``pattern`` in their body.
CONDITION_KINDS = frozenset({"override", "underride"})
PATTERN_KINDS = frozenset({"content"})

def _notify_sound() -> list[Any]:
    return ["notify", {"set_tweak": "sound", "value": "default"}]


def _notify_sound_highlight() -> list[Any]:
    return [*_notify_sound(), {"set_tweak": "highlight"}]


def _notify_highlight() -> list[Any]:
    return ["notify", {"set_tweak": "highlight"}]


def _rule(
    rule_id: str,
    *,
    actions: list[Any],
    conditions: list[Any] | None = None,
    pattern: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "rule_id": rule_id,
        "default": True,
        "enabled": enabled,
        "actions": actions,
    }
    if conditions is not None:
        rule["conditions"] = conditions
    if pattern is not None:
        rule["pattern"] = pattern
    return rule


def _event_match(key: str, pattern: str) -> dict[str, str]:
    return {"kind": "event_match", "key": key, "pattern": pattern}


def default_ruleset(user_id: str) -> dict[str, list[dict[str, Any]]]:
    """The spec's predefined push ruleset, instantiated for ``user_id``."""
    localpart = user_id.split(":", 1)[0].lstrip("@")
    override = [
        # The master switch: exists for every user but is disabled by default;
        # enabling it silences everything (its empty actions match all events).
        _rule(".m.rule.master", enabled=False, actions=[], conditions=[]),
        _rule(
            ".m.rule.suppress_notices",
            actions=[],
            conditions=[_event_match("content.msgtype", "m.notice")],
        ),
        _rule(
            ".m.rule.invite_for_me",
            actions=_notify_sound(),
            conditions=[
                _event_match("type", "m.room.member"),
                _event_match("content.membership", "invite"),
                _event_match("state_key", user_id),
            ],
        ),
        _rule(
            ".m.rule.member_event",
            actions=[],
            conditions=[_event_match("type", "m.room.member")],
        ),
        _rule(
            ".m.rule.contains_display_name",
            actions=_notify_sound_highlight(),
            conditions=[{"kind": "contains_display_name"}],
        ),
        _rule(
            ".m.rule.roomnotif",
            actions=_notify_highlight(),
            conditions=[
                _event_match("content.body", "@room"),
                {"kind": "sender_notification_permission", "key": "room"},
            ],
        ),
        _rule(
            ".m.rule.tombstone",
            actions=_notify_highlight(),
            conditions=[
                _event_match("type", "m.room.tombstone"),
                _event_match("state_key", ""),
            ],
        ),
    ]
    content = [
        _rule(
            ".m.rule.contains_user_name",
            pattern=localpart,
            actions=_notify_sound_highlight(),
        ),
    ]
    underride = [
        _rule(
            ".m.rule.call",
            actions=["notify", {"set_tweak": "sound", "value": "ring"}],
            conditions=[_event_match("type", "m.call.invite")],
        ),
        _rule(
            ".m.rule.encrypted_room_one_to_one",
            actions=_notify_sound(),
            conditions=[
                {"kind": "room_member_count", "is": "2"},
                _event_match("type", "m.room.encrypted"),
            ],
        ),
        _rule(
            ".m.rule.room_one_to_one",
            actions=_notify_sound(),
            conditions=[
                {"kind": "room_member_count", "is": "2"},
                _event_match("type", "m.room.message"),
            ],
        ),
        _rule(
            ".m.rule.message",
            actions=["notify"],
            conditions=[_event_match("type", "m.room.message")],
        ),
        _rule(
            ".m.rule.encrypted",
            actions=["notify"],
            conditions=[_event_match("type", "m.room.encrypted")],
        ),
    ]
    return {
        "override": override,
        "content": content,
        "room": [],
        "sender": [],
        "underride": underride,
    }


def _custom_rule_dict(row: PushRuleRow) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "rule_id": row.rule_id,
        "default": False,
        "enabled": True if row.enabled is None else row.enabled,
        "actions": row.actions if row.actions is not None else [],
    }
    if row.kind in CONDITION_KINDS:
        rule["conditions"] = row.conditions if row.conditions is not None else []
    if row.kind in PATTERN_KINDS:
        rule["pattern"] = row.pattern if row.pattern is not None else ""
    return rule


def _apply_override(rule: dict[str, Any], row: PushRuleRow | None) -> dict[str, Any]:
    if row is None:
        return rule
    merged = dict(rule)
    if row.enabled is not None:
        merged["enabled"] = row.enabled
    if row.actions is not None:
        merged["actions"] = row.actions
    return merged


def merged_ruleset(user_id: str, rows: list[PushRuleRow]) -> dict[str, list[dict[str, Any]]]:
    """Merge a user's stored rows into the server-default ruleset.

    Stored dot-rules only override ``enabled``/``actions`` of a matching default;
    custom rules slot in above the server defaults (but below ``.m.rule.master``).
    """
    defaults = default_ruleset(user_id)
    dot_overrides = {(r.kind, r.rule_id): r for r in rows if r.rule_id.startswith(".")}
    customs: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(
        (r for r in rows if not r.rule_id.startswith(".")),
        key=lambda r: (r.ordering, r.rule_id),
    ):
        customs.setdefault(row.kind, []).append(_custom_rule_dict(row))

    result: dict[str, list[dict[str, Any]]] = {}
    for kind in KINDS:
        base = [
            _apply_override(rule, dot_overrides.get((kind, rule["rule_id"])))
            for rule in defaults[kind]
        ]
        custom = customs.get(kind, [])
        if kind == "override" and base:
            # .m.rule.master stays on top; user rules outrank the other defaults.
            result[kind] = base[:1] + custom + base[1:]
        else:
            result[kind] = custom + base
    return result


def find_rule(
    ruleset: dict[str, list[dict[str, Any]]], kind: str, rule_id: str
) -> dict[str, Any] | None:
    for rule in ruleset.get(kind, []):
        if rule["rule_id"] == rule_id:
            return rule
    return None
