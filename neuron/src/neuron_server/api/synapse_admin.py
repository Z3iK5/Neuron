# SPDX-License-Identifier: Apache-2.0
"""The Synapse-compatible Admin API (``/_synapse/admin/...``) for neuron_server.

The path namespace is kept verbatim (the on-the-wire compatibility contract) so
the Neuron console, supervisor and other tooling work unchanged. Every endpoint
requires a **server-admin** access token.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse

from neuron_server.admin.service import AdminService
from neuron_server.api.deps import require_admin
from neuron_server.auth.service import Authenticated
from neuron_server.errors import MatrixError
from neuron_server.rooms.service import RoomService

router = APIRouter(prefix="/_synapse/admin")


def get_admin(request: Request) -> AdminService:
    service: AdminService = request.app.state.admin
    return service


def get_rooms(request: Request) -> RoomService:
    service: RoomService = request.app.state.rooms
    return service


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MatrixError(400, "M_NOT_JSON", "Request body is not valid JSON") from exc
    return data if isinstance(data, dict) else {}


def _int_param(request: Request, key: str, default: int) -> int:
    try:
        return int(request.query_params.get(key, str(default)))
    except ValueError:
        return default


def _bool_param(request: Request, key: str) -> bool | None:
    value = request.query_params.get(key)
    if value is None:
        return None
    return value.lower() == "true"


# --- server ----------------------------------------------------------------


@router.get("/v1/server_version")
async def server_version(
    who: Authenticated = Depends(require_admin), admin: AdminService = Depends(get_admin)
) -> dict[str, Any]:
    return admin.server_version()


# --- users -----------------------------------------------------------------


@router.get("/v2/users")
async def list_users(
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.list_users(
        offset=_int_param(request, "from", 0),
        limit=_int_param(request, "limit", 100),
        name=request.query_params.get("name"),
        deactivated=_bool_param(request, "deactivated"),
    )


@router.get("/v2/users/{user_id}")
async def get_user(
    user_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.get_user(user_id)


@router.put("/v2/users/{user_id}")
async def upsert_user(
    user_id: str,
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> JSONResponse:
    user, created = await admin.upsert_user(user_id, await _json_body(request))
    return JSONResponse(status_code=201 if created else 200, content=user)


@router.post("/v1/deactivate/{user_id}")
async def deactivate_user(
    user_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.deactivate_user(user_id)


@router.post("/v1/reset_password/{user_id}")
async def reset_password(
    user_id: str,
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    body = await _json_body(request)
    new_password = body.get("new_password")
    if not isinstance(new_password, str) or not new_password:
        raise MatrixError(400, "M_MISSING_PARAM", "Missing new_password")
    return await admin.reset_password(user_id, new_password)


@router.post("/v1/users/{user_id}/shadow_ban")
async def shadow_ban(
    user_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.set_shadow_ban(user_id, True)


@router.delete("/v1/users/{user_id}/shadow_ban")
async def shadow_unban(
    user_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.set_shadow_ban(user_id, False)


@router.post("/v1/user/{user_id}/redact")
async def redact_user(
    user_id: str,
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    body = await _json_body(request)
    rooms = body.get("rooms")
    rooms_list = [str(r) for r in rooms] if isinstance(rooms, list) else None
    return await admin.redact_user_events(user_id, rooms=rooms_list)


@router.get("/v1/user/redact_status/{redact_id}")
async def redact_status(
    redact_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.get_redact_status(redact_id)


# --- rooms -----------------------------------------------------------------


@router.get("/v1/rooms")
async def list_rooms(
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.list_rooms(
        offset=_int_param(request, "from", 0), limit=_int_param(request, "limit", 100)
    )


@router.get("/v1/rooms/{room_id}/members")
async def room_members(
    room_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.get_room_members(room_id)


@router.get("/v1/rooms/{room_id}/state")
async def room_state(
    room_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.get_room_state(room_id)


@router.post("/v1/rooms/{room_id}/make_room_admin")
async def make_room_admin(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_admin),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await _json_body(request)
    raw = body.get("user_id")
    target = raw if isinstance(raw, str) else who.user_id
    await rooms.admin_make_room_admin(room_id, target)
    return {}


@router.put("/v1/rooms/{room_id}/block")
async def room_block(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    body = await _json_body(request)
    return await admin.set_room_block(room_id, bool(body.get("block", True)))


@router.delete("/v2/rooms/{room_id}")
async def delete_room(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    body = await _json_body(request)
    return await admin.delete_room(
        room_id, block=bool(body.get("block", False)), purge=bool(body.get("purge", True))
    )


@router.get("/v2/rooms/delete_status/{delete_id}")
async def delete_status(
    delete_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.get_delete_status(delete_id)


@router.get("/v1/rooms/{room_id}")
async def get_room(
    room_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.get_room(room_id)


@router.post("/v1/join/{room_id_or_alias}")
async def force_join(
    room_id_or_alias: str,
    request: Request,
    who: Authenticated = Depends(require_admin),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await _json_body(request)
    target = body.get("user_id")
    if not isinstance(target, str):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing user_id")
    if not room_id_or_alias.startswith("!"):
        raise MatrixError(400, "M_INVALID_PARAM", "Room aliases are not supported yet")
    await rooms.admin_force_join(room_id_or_alias, target)
    return {"room_id": room_id_or_alias}


# --- registration tokens / reports ----------------------------------------


@router.get("/v1/registration_tokens")
async def list_registration_tokens(
    who: Authenticated = Depends(require_admin), admin: AdminService = Depends(get_admin)
) -> dict[str, Any]:
    return await admin.list_registration_tokens()


@router.post("/v1/registration_tokens/new")
async def new_registration_token(
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    body = await _json_body(request)
    return await admin.create_registration_token(
        token=body.get("token"),
        uses_allowed=body.get("uses_allowed"),
        expiry_time=body.get("expiry_time"),
    )


@router.delete("/v1/registration_tokens/{token}")
async def delete_registration_token(
    token: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.delete_registration_token(token)


@router.get("/v1/event_reports")
async def event_reports(
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.list_event_reports(
        offset=_int_param(request, "from", 0), limit=_int_param(request, "limit", 100)
    )


@router.get("/v1/event_reports/{report_id}")
async def event_report(
    report_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.get_event_report(report_id)


@router.delete("/v1/event_reports/{report_id}")
async def delete_event_report(
    report_id: str,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    return await admin.delete_event_report(report_id)


@router.post("/v1/send_server_notice")
async def send_server_notice(
    request: Request,
    who: Authenticated = Depends(require_admin),
    admin: AdminService = Depends(get_admin),
) -> dict[str, Any]:
    body = await _json_body(request)
    user_id = str(body.get("user_id") or "")
    if not user_id:
        raise MatrixError(400, "M_MISSING_PARAM", "user_id is required")
    content = body.get("content")
    if not isinstance(content, dict):
        raise MatrixError(400, "M_BAD_JSON", "content must be an object")
    state_key = body.get("state_key")
    return await admin.send_server_notice(
        user_id,
        content,
        event_type=str(body.get("type") or "m.room.message"),
        state_key=None if state_key is None else str(state_key),
    )
