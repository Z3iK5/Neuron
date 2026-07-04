# SPDX-License-Identifier: Apache-2.0
"""Data access for per-user push rules.

The ``push_rules`` table stores only what differs from the computed server
defaults: full custom rules (rule ids not starting with ``.``) and per-rule
``enabled``/``actions`` overrides of the predefined ``.m.*`` rules. Merging
with the defaults happens in :mod:`neuron_server.pushrules`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from neuron_server.storage.database import Database


@dataclass(frozen=True)
class PushRuleRow:
    kind: str
    rule_id: str
    ordering: int
    conditions: list[Any] | None
    actions: list[Any] | None
    pattern: str | None
    enabled: bool | None  # None = no override (use the rule's default)


def _row(row: tuple[Any, ...]) -> PushRuleRow:
    kind, rule_id, ordering, conditions_json, actions_json, pattern, enabled = row
    return PushRuleRow(
        kind=str(kind),
        rule_id=str(rule_id),
        ordering=int(ordering),
        conditions=None if conditions_json is None else json.loads(str(conditions_json)),
        actions=None if actions_json is None else json.loads(str(actions_json)),
        pattern=None if pattern is None else str(pattern),
        enabled=None if enabled is None else bool(enabled),
    )


_COLUMNS = "kind, rule_id, ordering, conditions_json, actions_json, pattern, enabled"


async def get_rules(db: Database, user_id: str) -> list[PushRuleRow]:
    rows = await db.fetchall(
        f"SELECT {_COLUMNS} FROM push_rules WHERE user_id = ? ORDER BY kind, ordering, rule_id",
        (user_id,),
    )
    return [_row(row) for row in rows]


async def get_rule(db: Database, user_id: str, kind: str, rule_id: str) -> PushRuleRow | None:
    rows = await db.fetchall(
        f"SELECT {_COLUMNS} FROM push_rules WHERE user_id = ? AND kind = ? AND rule_id = ?",
        (user_id, kind, rule_id),
    )
    return _row(rows[0]) if rows else None


async def upsert_rule(
    db: Database,
    user_id: str,
    kind: str,
    rule_id: str,
    *,
    ordering: int,
    conditions: list[Any] | None,
    actions: list[Any],
    pattern: str | None,
) -> None:
    """Create or replace a custom rule's definition (an existing ``enabled``
    override survives the update, per the spec's enabled/actions endpoints)."""
    await db.execute(
        "INSERT INTO push_rules"
        " (user_id, kind, rule_id, ordering, conditions_json, actions_json, pattern)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, kind, rule_id) DO UPDATE SET"
        " ordering = excluded.ordering, conditions_json = excluded.conditions_json,"
        " actions_json = excluded.actions_json, pattern = excluded.pattern",
        (
            user_id,
            kind,
            rule_id,
            ordering,
            None if conditions is None else json.dumps(conditions),
            json.dumps(actions),
            pattern,
        ),
    )


async def set_rule_enabled(
    db: Database, user_id: str, kind: str, rule_id: str, enabled: bool
) -> None:
    """Upsert only the ``enabled`` flag (works for defaults and custom rules)."""
    await db.execute(
        "INSERT INTO push_rules (user_id, kind, rule_id, ordering, enabled)"
        " VALUES (?, ?, ?, 0, ?)"
        " ON CONFLICT(user_id, kind, rule_id) DO UPDATE SET enabled = excluded.enabled",
        (user_id, kind, rule_id, 1 if enabled else 0),
    )


async def set_rule_actions(
    db: Database, user_id: str, kind: str, rule_id: str, actions: list[Any]
) -> None:
    """Upsert only the ``actions`` list (works for defaults and custom rules)."""
    await db.execute(
        "INSERT INTO push_rules (user_id, kind, rule_id, ordering, actions_json)"
        " VALUES (?, ?, ?, 0, ?)"
        " ON CONFLICT(user_id, kind, rule_id) DO UPDATE SET actions_json = excluded.actions_json",
        (user_id, kind, rule_id, json.dumps(actions)),
    )


async def set_rule_ordering(
    db: Database, user_id: str, kind: str, rule_id: str, ordering: int
) -> None:
    await db.execute(
        "UPDATE push_rules SET ordering = ? WHERE user_id = ? AND kind = ? AND rule_id = ?",
        (ordering, user_id, kind, rule_id),
    )


async def delete_rule(db: Database, user_id: str, kind: str, rule_id: str) -> None:
    await db.execute(
        "DELETE FROM push_rules WHERE user_id = ? AND kind = ? AND rule_id = ?",
        (user_id, kind, rule_id),
    )
