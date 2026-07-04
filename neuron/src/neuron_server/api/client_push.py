# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: push rules.

Serves the spec's predefined server-default ruleset merged with the user's
stored custom rules and per-rule ``enabled``/``actions`` overrides. Every
change re-publishes the merged ruleset as ``m.push_rules`` account data (and
wakes /sync), which is how Element learns to refresh its rules.

Only the ``global`` scope exists (the spec defines no other). Pushers/actual
push delivery are out of scope — these rules exist so clients can evaluate
notifications locally.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server import pushrules
from neuron_server.api.deps import json_body, require_user
from neuron_server.auth.service import Authenticated
from neuron_server.errors import MatrixError
from neuron_server.storage import push as push_store
from neuron_server.storage import userdata
from neuron_server.storage.database import Database

router = APIRouter(prefix="/_matrix/client")


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def _check_scope(scope: str) -> None:
    if scope != "global":
        raise MatrixError(400, "M_INVALID_PARAM", f"Unknown push-rule scope {scope!r}")


def _check_kind(kind: str) -> None:
    if kind not in pushrules.KINDS:
        raise MatrixError(400, "M_INVALID_PARAM", f"Unknown push-rule kind {kind!r}")


async def _ruleset(db: Database, user_id: str) -> dict[str, list[dict[str, Any]]]:
    rows = await push_store.get_rules(db, user_id)
    return pushrules.merged_ruleset(user_id, rows)


async def _find_rule(
    db: Database, user_id: str, scope: str, kind: str, rule_id: str
) -> dict[str, Any]:
    _check_scope(scope)
    _check_kind(kind)
    rule = pushrules.find_rule(await _ruleset(db, user_id), kind, rule_id)
    if rule is None:
        raise MatrixError(404, "M_NOT_FOUND", "No push rule with that ID")
    return rule


async def _publish_rules(request: Request, db: Database, user_id: str) -> None:
    """Re-publish the merged ruleset as ``m.push_rules`` account data.

    The account-data stream carries it into the next incremental /sync, so
    clients refresh their rules; the notifier wakes any long-poller now.
    """
    ruleset = await _ruleset(db, user_id)
    await userdata.set_account_data(db, user_id, "", "m.push_rules", {"global": ruleset})
    request.app.state.notify()


# --- reading ----------------------------------------------------------------


# The spec's path is /pushrules/ (trailing slash); serve the bare path too so a
# client that strips the slash isn't redirected.
@router.get("/v3/pushrules")
@router.get("/v3/pushrules/")
async def get_all_push_rules(
    who: Authenticated = Depends(require_user), db: Database = Depends(get_db)
) -> dict[str, Any]:
    return {"global": await _ruleset(db, who.user_id)}


@router.get("/v3/pushrules/{scope}")
async def get_scoped_push_rules(
    scope: str,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _check_scope(scope)
    return await _ruleset(db, who.user_id)


@router.get("/v3/pushrules/{scope}/{kind}/{rule_id}")
async def get_push_rule(
    scope: str,
    kind: str,
    rule_id: str,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    return await _find_rule(db, who.user_id, scope, kind, rule_id)


# --- creating / deleting custom rules ---------------------------------------


def _validate_rule_body(
    kind: str, body: dict[str, Any]
) -> tuple[list[Any], list[Any] | None, str | None]:
    actions = body.get("actions")
    if not isinstance(actions, list):
        raise MatrixError(400, "M_MISSING_PARAM", "Push rules must have actions")
    conditions: list[Any] | None = None
    pattern: str | None = None
    if kind in pushrules.CONDITION_KINDS:
        conditions = body.get("conditions", [])
        if not isinstance(conditions, list):
            raise MatrixError(400, "M_INVALID_PARAM", "conditions must be a list")
    if kind in pushrules.PATTERN_KINDS:
        pattern = body.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise MatrixError(400, "M_MISSING_PARAM", "Content rules must have a pattern")
    return actions, conditions, pattern


async def _place_rule(
    db: Database,
    user_id: str,
    kind: str,
    rule_id: str,
    *,
    before: str | None,
    after: str | None,
) -> int:
    """Pick the new rule's ordering slot, renumbering siblings as needed.

    Custom rules of a kind are kept in a small dense sequence, so we can just
    rewrite every sibling's ordering — family-scale rule counts make this cheap.
    """
    siblings = [
        row
        for row in await push_store.get_rules(db, user_id)
        if row.kind == kind and not row.rule_id.startswith(".") and row.rule_id != rule_id
    ]
    order = [row.rule_id for row in siblings]
    anchor = before or after
    if anchor is not None:
        if anchor not in order:
            raise MatrixError(400, "M_UNKNOWN", "before/after rule not found")
        index = order.index(anchor) + (0 if before else 1)
    else:
        index = len(order)
    order.insert(index, rule_id)
    for position, sibling_id in enumerate(order):
        if sibling_id != rule_id:
            await push_store.set_rule_ordering(db, user_id, kind, sibling_id, position)
    return index


@router.put("/v3/pushrules/{scope}/{kind}/{rule_id}")
async def put_push_rule(
    scope: str,
    kind: str,
    rule_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _check_scope(scope)
    _check_kind(kind)
    if rule_id.startswith("."):
        raise MatrixError(
            400, "M_UNKNOWN", "Cannot create or modify server-default rules; use"
            " the enabled/actions endpoints to tweak them"
        )
    body = await json_body(request)
    actions, conditions, pattern = _validate_rule_body(kind, body)
    before = request.query_params.get("before")
    after = request.query_params.get("after")
    existing = await push_store.get_rule(db, who.user_id, kind, rule_id)
    if existing is not None and before is None and after is None:
        ordering = existing.ordering  # plain update keeps the rule's position
    else:
        ordering = await _place_rule(
            db, who.user_id, kind, rule_id, before=before, after=after
        )
    await push_store.upsert_rule(
        db,
        who.user_id,
        kind,
        rule_id,
        ordering=ordering,
        conditions=conditions,
        actions=actions,
        pattern=pattern,
    )
    await _publish_rules(request, db, who.user_id)
    return {}


@router.delete("/v3/pushrules/{scope}/{kind}/{rule_id}")
async def delete_push_rule(
    scope: str,
    kind: str,
    rule_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _check_scope(scope)
    _check_kind(kind)
    # Server-default rules can't be deleted: like the rest of the spec, "not a
    # deletable rule" surfaces as the 404 the DELETE endpoint documents.
    if rule_id.startswith(".") or await push_store.get_rule(db, who.user_id, kind, rule_id) is None:
        raise MatrixError(404, "M_NOT_FOUND", "No deletable push rule with that ID")
    await push_store.delete_rule(db, who.user_id, kind, rule_id)
    await _publish_rules(request, db, who.user_id)
    return {}


# --- enabled / actions (work on predefined rules too) ------------------------


@router.get("/v3/pushrules/{scope}/{kind}/{rule_id}/enabled")
async def get_push_rule_enabled(
    scope: str,
    kind: str,
    rule_id: str,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    rule = await _find_rule(db, who.user_id, scope, kind, rule_id)
    return {"enabled": rule["enabled"]}


@router.put("/v3/pushrules/{scope}/{kind}/{rule_id}/enabled")
async def set_push_rule_enabled(
    scope: str,
    kind: str,
    rule_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    await _find_rule(db, who.user_id, scope, kind, rule_id)  # 404 if unknown
    body = await json_body(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise MatrixError(400, "M_INVALID_PARAM", "enabled must be a boolean")
    await push_store.set_rule_enabled(db, who.user_id, kind, rule_id, enabled)
    await _publish_rules(request, db, who.user_id)
    return {}


@router.get("/v3/pushrules/{scope}/{kind}/{rule_id}/actions")
async def get_push_rule_actions(
    scope: str,
    kind: str,
    rule_id: str,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    rule = await _find_rule(db, who.user_id, scope, kind, rule_id)
    return {"actions": rule["actions"]}


@router.put("/v3/pushrules/{scope}/{kind}/{rule_id}/actions")
async def set_push_rule_actions(
    scope: str,
    kind: str,
    rule_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    await _find_rule(db, who.user_id, scope, kind, rule_id)  # 404 if unknown
    body = await json_body(request)
    actions = body.get("actions")
    if not isinstance(actions, list):
        raise MatrixError(400, "M_MISSING_PARAM", "actions must be a list")
    await push_store.set_rule_actions(db, who.user_id, kind, rule_id, actions)
    await _publish_rules(request, db, who.user_id)
    return {}
