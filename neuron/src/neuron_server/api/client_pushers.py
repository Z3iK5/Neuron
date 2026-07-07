# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: pushers and the notifications list.

``/pushers`` registers where a user's push notifications are delivered (a phone's
device token plus its push gateway URL); ``/notifications`` lists the events the
user's push rules have generated notifications for. Actual delivery to the gateway
happens off the request path in :mod:`neuron_server.push.sender`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server.api.deps import json_body, require_user
from neuron_server.auth.service import Authenticated
from neuron_server.clock import now_ms
from neuron_server.errors import MatrixError
from neuron_server.storage import notifications as notif_store
from neuron_server.storage import pushers as pusher_store
from neuron_server.storage.database import Database

router = APIRouter(prefix="/_matrix/client")


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


@router.get("/v3/pushers")
async def get_pushers(
    who: Authenticated = Depends(require_user), db: Database = Depends(get_db)
) -> dict[str, Any]:
    pushers = await pusher_store.get_pushers(db, who.user_id)
    return {"pushers": [p.to_client() for p in pushers]}


@router.post("/v3/pushers/set")
async def set_pusher(
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    body = await json_body(request)
    app_id = body.get("app_id")
    pushkey = body.get("pushkey")
    if not isinstance(app_id, str) or not isinstance(pushkey, str):
        raise MatrixError(400, "M_MISSING_PARAM", "app_id and pushkey are required")

    kind = body.get("kind")
    # kind == null deletes the pusher (spec); otherwise create/update it.
    if kind is None:
        await pusher_store.delete_pusher(db, who.user_id, app_id, pushkey)
        return {}
    if kind != "http":
        raise MatrixError(400, "M_UNKNOWN", f"Unsupported pusher kind {kind!r}")

    data = body.get("data")
    if not isinstance(data, dict):
        raise MatrixError(400, "M_MISSING_PARAM", "data is required")
    if not isinstance(data.get("url"), str):
        raise MatrixError(400, "M_MISSING_PARAM", "An http pusher must have data.url")

    # append=false (the default) means this device token belongs to one user only:
    # remove the same pushkey wherever else it is registered.
    if not body.get("append", False):
        await pusher_store.delete_pushkey_elsewhere(db, who.user_id, app_id, pushkey)

    await pusher_store.upsert_pusher(
        db,
        who.user_id,
        app_id=app_id,
        pushkey=pushkey,
        kind=kind,
        app_display_name=_opt_str(body.get("app_display_name")),
        device_display_name=_opt_str(body.get("device_display_name")),
        profile_tag=_opt_str(body.get("profile_tag")),
        lang=_opt_str(body.get("lang")),
        data=data,
        ts=now_ms(),
    )
    return {}


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


@router.get("/v3/notifications")
async def get_notifications(
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    params = request.query_params
    limit = _parse_int(params.get("limit"), default=100, maximum=1000) or 100
    from_ts = _parse_int(params.get("from"), default=None)
    only_highlight = params.get("only") == "highlight"

    entries, next_from = await notif_store.list_for_user(
        db, who.user_id, limit=limit, from_ts=from_ts, only_highlight=only_highlight
    )
    notifications = [
        {
            "event": n.event.client_dict(),
            "room_id": n.room_id,
            "actions": n.actions,
            "read": read,
            "ts": n.ts,
        }
        for n, read in entries
    ]
    result: dict[str, Any] = {"notifications": notifications}
    if next_from is not None:
        result["next_token"] = str(next_from)
    return result


def _parse_int(
    raw: str | None, *, default: int | None, maximum: int | None = None
) -> int | None:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise MatrixError(400, "M_INVALID_PARAM", "Invalid integer parameter") from exc
    if maximum is not None:
        value = min(value, maximum)
    return max(0, value)
