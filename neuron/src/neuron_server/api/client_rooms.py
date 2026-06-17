# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: rooms, events, state, membership, history (HS-2).

Implements room creation, sending message/state events, membership changes,
redactions, and reading state/events/history. Authorization is enforced by
:mod:`neuron_server.rooms.authrules`.

Not yet covered (honest scope): room aliases/directory, knock/restricted joins,
guests, third-party invites, room upgrades, and event filtering on /messages.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse

from neuron_server.api.deps import require_user
from neuron_server.auth.service import Authenticated
from neuron_server.errors import MatrixError
from neuron_server.rooms.service import RoomService

router = APIRouter(prefix="/_matrix/client")


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
    if not isinstance(data, dict):
        raise MatrixError(400, "M_BAD_JSON", "Request body must be a JSON object")
    return data


# --- create ----------------------------------------------------------------


@router.post("/v3/createRoom")
async def create_room(
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await _json_body(request)
    room_id = await rooms.create_room(who.user_id, body)
    return {"room_id": room_id}


# --- sending events --------------------------------------------------------


@router.put("/v3/rooms/{room_id}/send/{event_type}/{txn_id}")
async def send_message(
    room_id: str,
    event_type: str,
    txn_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    content = await _json_body(request)
    event_id = await rooms.send_message(room_id, who.user_id, event_type, content, txn_id)
    return {"event_id": event_id}


@router.put("/v3/rooms/{room_id}/state/{event_type}/{state_key:path}")
async def send_state_with_key(
    room_id: str,
    event_type: str,
    state_key: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    content = await _json_body(request)
    event_id = await rooms.send_state(room_id, who.user_id, event_type, state_key, content)
    return {"event_id": event_id}


@router.put("/v3/rooms/{room_id}/state/{event_type}")
async def send_state_no_key(
    room_id: str,
    event_type: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    content = await _json_body(request)
    event_id = await rooms.send_state(room_id, who.user_id, event_type, "", content)
    return {"event_id": event_id}


# --- membership ------------------------------------------------------------


@router.post("/v3/join/{room_id}")
async def join_by_id(
    room_id: str,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    if not room_id.startswith("!"):
        raise MatrixError(400, "M_INVALID_PARAM", "Room aliases are not supported yet")
    return {"room_id": await rooms.join(room_id, who.user_id)}


@router.post("/v3/rooms/{room_id}/join")
async def join_room(
    room_id: str,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return {"room_id": await rooms.join(room_id, who.user_id)}


@router.post("/v3/rooms/{room_id}/leave")
async def leave_room(
    room_id: str,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    await rooms.leave(room_id, who.user_id)
    return {}


@router.post("/v3/rooms/{room_id}/invite")
async def invite_to_room(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await _json_body(request)
    target = body.get("user_id")
    if not isinstance(target, str):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing user_id")
    await rooms.invite(room_id, who.user_id, target)
    return {}


@router.post("/v3/rooms/{room_id}/kick")
async def kick_from_room(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await _json_body(request)
    target = body.get("user_id")
    if not isinstance(target, str):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing user_id")
    await rooms.kick(room_id, who.user_id, target, body.get("reason"))
    return {}


@router.post("/v3/rooms/{room_id}/ban")
async def ban_from_room(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await _json_body(request)
    target = body.get("user_id")
    if not isinstance(target, str):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing user_id")
    await rooms.ban(room_id, who.user_id, target, body.get("reason"))
    return {}


@router.post("/v3/rooms/{room_id}/unban")
async def unban_from_room(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await _json_body(request)
    target = body.get("user_id")
    if not isinstance(target, str):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing user_id")
    await rooms.unban(room_id, who.user_id, target)
    return {}


# --- redaction -------------------------------------------------------------


@router.put("/v3/rooms/{room_id}/redact/{event_id}/{txn_id}")
async def redact_event(
    room_id: str,
    event_id: str,
    txn_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await _json_body(request)
    redaction_id = await rooms.redact(room_id, who.user_id, event_id, txn_id, body.get("reason"))
    return {"event_id": redaction_id}


# --- reads -----------------------------------------------------------------


@router.get("/v3/joined_rooms")
async def joined_rooms(
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return {"joined_rooms": await rooms.joined_rooms(who.user_id)}


@router.get("/v3/rooms/{room_id}/state")
async def get_room_state(
    room_id: str,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> JSONResponse:
    return JSONResponse(content=await rooms.get_state_events(room_id))


@router.get("/v3/rooms/{room_id}/state/{event_type}/{state_key:path}")
async def get_state_with_key(
    room_id: str,
    event_type: str,
    state_key: str,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return await rooms.get_state_content(room_id, event_type, state_key)


@router.get("/v3/rooms/{room_id}/state/{event_type}")
async def get_state_no_key(
    room_id: str,
    event_type: str,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return await rooms.get_state_content(room_id, event_type, "")


@router.get("/v3/rooms/{room_id}/event/{event_id}")
async def get_one_event(
    room_id: str,
    event_id: str,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return await rooms.get_event(room_id, event_id)


@router.get("/v3/rooms/{room_id}/joined_members")
async def get_joined_members(
    room_id: str,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return {"joined": await rooms.joined_members(room_id)}


async def _messages(
    room_id: str, request: Request, rooms: RoomService
) -> dict[str, Any]:
    direction = request.query_params.get("dir", "b")
    if direction not in ("b", "f"):
        raise MatrixError(400, "M_INVALID_PARAM", "dir must be 'b' or 'f'")
    limit_param = request.query_params.get("limit", "10")
    try:
        limit = int(limit_param)
    except ValueError as exc:
        raise MatrixError(400, "M_INVALID_PARAM", "limit must be an integer") from exc
    return await rooms.get_messages(
        room_id, from_token=request.query_params.get("from"), direction=direction, limit=limit
    )


# /messages is served under both v3 (spec) and v1 (used by neuron_core's client).
@router.get("/v3/rooms/{room_id}/messages")
async def get_messages_v3(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return await _messages(room_id, request, rooms)


@router.get("/v1/rooms/{room_id}/messages")
async def get_messages_v1(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return await _messages(room_id, request, rooms)
