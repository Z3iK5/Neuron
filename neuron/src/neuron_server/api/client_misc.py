# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: profile, account data, capabilities, filters, and the
typing/receipt/presence surfaces (HS-6).

These round out the everyday client API. Presence is accepted but not distributed;
push rules live in :mod:`neuron_server.api.client_push`. Profile, account data,
filters, typing, read receipts and read markers are fully stored (and surfaced
via /sync).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server.api.deps import json_body, require_user
from neuron_server.auth.service import Authenticated
from neuron_server.errors import MatrixError
from neuron_server.rooms import versions
from neuron_server.storage import receipts as receipts_store
from neuron_server.storage import userdata
from neuron_server.storage.database import Database

router = APIRouter(prefix="/_matrix/client")


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def _require_self(who: Authenticated, user_id: str) -> None:
    if who.user_id != user_id:
        raise MatrixError(403, "M_FORBIDDEN", "Cannot modify another user's data")


# --- profile ---------------------------------------------------------------


async def _profile_any(request: Request, db: Database, user_id: str) -> dict[str, Any]:
    """A local user's stored profile, or a remote user's fetched over federation."""
    if user_id.split(":", 1)[-1] != request.app.state.settings.name:
        return await request.app.state.remote_profiles.fetch(user_id)
    return await userdata.get_profile(db, user_id)


@router.get("/v3/profile/{user_id}")
async def get_profile(
    user_id: str, request: Request, db: Database = Depends(get_db)
) -> dict[str, Any]:
    return await _profile_any(request, db, user_id)


@router.get("/v3/profile/{user_id}/displayname")
async def get_displayname(
    user_id: str, request: Request, db: Database = Depends(get_db)
) -> dict[str, Any]:
    profile = await _profile_any(request, db, user_id)
    return {"displayname": profile.get("displayname")}


@router.put("/v3/profile/{user_id}/displayname")
async def set_displayname(
    user_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_self(who, user_id)
    body = await json_body(request)
    await userdata.set_displayname(db, user_id, body.get("displayname"))
    return {}


@router.get("/v3/profile/{user_id}/avatar_url")
async def get_avatar_url(
    user_id: str, request: Request, db: Database = Depends(get_db)
) -> dict[str, Any]:
    profile = await _profile_any(request, db, user_id)
    return {"avatar_url": profile.get("avatar_url")}


@router.put("/v3/profile/{user_id}/avatar_url")
async def set_avatar_url(
    user_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_self(who, user_id)
    body = await json_body(request)
    await userdata.set_avatar_url(db, user_id, body.get("avatar_url"))
    return {}


# --- account data ----------------------------------------------------------


@router.get("/v3/user/{user_id}/account_data/{data_type}")
async def get_global_account_data(
    user_id: str,
    data_type: str,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_self(who, user_id)
    content = await userdata.get_account_data(db, user_id, "", data_type)
    if content is None:
        raise MatrixError(404, "M_NOT_FOUND", "Account data not found")
    return content


@router.put("/v3/user/{user_id}/account_data/{data_type}")
async def set_global_account_data(
    user_id: str,
    data_type: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_self(who, user_id)
    await userdata.set_account_data(db, user_id, "", data_type, await json_body(request))
    request.app.state.notify()  # wake long-polling /sync so the change roams
    return {}


@router.get("/v3/user/{user_id}/rooms/{room_id}/account_data/{data_type}")
async def get_room_account_data(
    user_id: str,
    room_id: str,
    data_type: str,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_self(who, user_id)
    content = await userdata.get_account_data(db, user_id, room_id, data_type)
    if content is None:
        raise MatrixError(404, "M_NOT_FOUND", "Account data not found")
    return content


@router.put("/v3/user/{user_id}/rooms/{room_id}/account_data/{data_type}")
async def set_room_account_data(
    user_id: str,
    room_id: str,
    data_type: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_self(who, user_id)
    await userdata.set_account_data(db, user_id, room_id, data_type, await json_body(request))
    request.app.state.notify()  # wake long-polling /sync so the change roams
    return {}


# --- filters ---------------------------------------------------------------


@router.post("/v3/user/{user_id}/filter")
async def create_filter(
    user_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_self(who, user_id)
    filter_id = str(await userdata.count_filters(db, user_id))
    await userdata.create_filter(db, user_id, filter_id, await json_body(request))
    return {"filter_id": filter_id}


@router.get("/v3/user/{user_id}/filter/{filter_id}")
async def get_filter(
    user_id: str,
    filter_id: str,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_self(who, user_id)
    definition = await userdata.get_filter(db, user_id, filter_id)
    if definition is None:
        raise MatrixError(404, "M_NOT_FOUND", "Filter not found")
    return definition


# --- capabilities -----------------------------------------------------------


@router.get("/v3/capabilities")
async def capabilities(who: Authenticated = Depends(require_user)) -> dict[str, Any]:
    return {
        "capabilities": {
            "m.change_password": {"enabled": True},
            "m.room_versions": {
                "default": versions.DEFAULT_ROOM_VERSION,
                "available": {v: "stable" for v in versions.SUPPORTED_ROOM_VERSIONS},
            },
        }
    }


# --- presence / typing / receipts (accepted, not yet distributed) ----------


@router.get("/v3/presence/{user_id}/status")
async def get_presence(user_id: str, who: Authenticated = Depends(require_user)) -> dict[str, Any]:
    return {"presence": "offline"}


@router.put("/v3/presence/{user_id}/status")
async def set_presence(
    user_id: str, who: Authenticated = Depends(require_user)
) -> dict[str, Any]:
    return {}


@router.put("/v3/rooms/{room_id}/typing/{user_id}")
async def typing(
    room_id: str,
    user_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
) -> dict[str, Any]:
    if user_id != who.user_id:
        raise MatrixError(403, "M_FORBIDDEN", "Cannot set another user's typing state")
    body = await json_body(request)
    is_typing = bool(body.get("typing"))
    try:
        timeout = int(body.get("timeout", 30000))
    except (TypeError, ValueError) as exc:
        raise MatrixError(400, "M_INVALID_PARAM", "timeout must be an integer") from exc
    await request.app.state.typing.set_typing(room_id, user_id, is_typing, timeout)
    await request.app.state.federation_sender.send_typing(room_id, user_id, is_typing)
    return {}


@router.post("/v3/rooms/{room_id}/receipt/{receipt_type}/{event_id}")
async def receipt(
    room_id: str,
    receipt_type: str,
    event_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    if receipt_type not in ("m.read", "m.read.private"):
        return {}  # only read receipts are persisted for now
    ts = int(time.time() * 1000)
    await receipts_store.upsert_receipt(db, room_id, who.user_id, receipt_type, event_id, ts)
    request.app.state.notify()
    if receipt_type == "m.read":  # private receipts never leave this server
        await request.app.state.federation_sender.send_receipt(
            room_id, who.user_id, receipt_type, event_id, ts
        )
    return {}


@router.post("/v3/rooms/{room_id}/read_markers")
async def read_markers(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    body = await json_body(request)
    wrote = False
    fully_read = body.get("m.fully_read")
    if isinstance(fully_read, str):
        # Stored as per-room account data so it roams to the user's other clients
        # via the /sync account-data stream.
        await userdata.set_account_data(
            db, who.user_id, room_id, "m.fully_read", {"event_id": fully_read}
        )
        wrote = True
    ts = int(time.time() * 1000)
    for receipt_type in ("m.read", "m.read.private"):
        event_id = body.get(receipt_type)
        if not isinstance(event_id, str):
            continue
        await receipts_store.upsert_receipt(
            db, room_id, who.user_id, receipt_type, event_id, ts
        )
        wrote = True
        if receipt_type == "m.read":  # private receipts never leave this server
            await request.app.state.federation_sender.send_receipt(
                room_id, who.user_id, receipt_type, event_id, ts
            )
    if wrote:
        request.app.state.notify()
    return {}
