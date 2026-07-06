# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: room aliases and the public room directory (HS-2).

Local aliases (``#localpart:server_name``) map human-readable names to room ids so
clients can discover and join rooms by name. The public directory lists rooms whose
per-room visibility flag is ``public``. Remote alias resolution is answered over
federation; cross-server directory *browsing* is out of scope (``/publicRooms``
lists only our own rooms).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server.api.deps import get_rooms, json_body, require_user
from neuron_server.auth.service import Authenticated
from neuron_server.errors import MatrixError
from neuron_server.federation.directory import resolve_remote_alias
from neuron_server.rooms.service import RoomService
from neuron_server.storage import directory as directory_store

router = APIRouter(prefix="/_matrix/client")


def _server_name(request: Request) -> str:
    name: str = request.app.state.settings.name
    return name


async def _resolve_alias(request: Request, alias: str) -> dict[str, Any]:
    """Resolve ``alias`` locally or over federation to ``{room_id, servers}``."""
    if not directory_store.is_valid_alias(alias):
        raise MatrixError(400, "M_INVALID_PARAM", "Invalid room alias")
    rooms: RoomService = request.app.state.rooms
    if directory_store.alias_server(alias) == _server_name(request):
        resolved = await rooms.resolve_local_alias(alias)
        if resolved is None:
            raise MatrixError(404, "M_NOT_FOUND", "Room alias not found")
        room_id, servers = resolved
    else:
        room_id, servers = await resolve_remote_alias(
            request.app.state.federation_client, alias
        )
    return {"room_id": room_id, "servers": servers}


# --- aliases ---------------------------------------------------------------


@router.put("/v3/directory/room/{room_alias:path}")
async def put_room_alias(
    room_alias: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await json_body(request)
    room_id = body.get("room_id")
    if not isinstance(room_id, str):
        raise MatrixError(400, "M_INVALID_PARAM", "Missing room_id")
    await rooms.create_room_alias(room_alias, room_id, who.user_id)
    return {}


@router.get("/v3/directory/room/{room_alias:path}")
async def get_room_alias(room_alias: str, request: Request) -> dict[str, Any]:
    return await _resolve_alias(request, room_alias)


@router.delete("/v3/directory/room/{room_alias:path}")
async def delete_room_alias(
    room_alias: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    if not directory_store.is_valid_alias(room_alias):
        raise MatrixError(400, "M_INVALID_PARAM", "Invalid room alias")
    if directory_store.alias_server(room_alias) != _server_name(request):
        raise MatrixError(400, "M_INVALID_PARAM", "Alias is for another server")
    await rooms.delete_room_alias(room_alias, who.user_id)
    return {}


# --- per-room published flag -----------------------------------------------


@router.get("/v3/directory/list/room/{room_id}")
async def get_room_visibility(
    room_id: str,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return {"visibility": await rooms.get_directory_visibility(room_id)}


@router.put("/v3/directory/list/room/{room_id}")
async def put_room_visibility(
    room_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await json_body(request)
    visibility = body.get("visibility")
    if not isinstance(visibility, str):
        raise MatrixError(400, "M_INVALID_PARAM", "Missing visibility")
    await rooms.set_directory_visibility(room_id, who.user_id, visibility)
    return {}


# --- public directory ------------------------------------------------------


def _int_param(raw: str | None, name: str) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise MatrixError(400, "M_INVALID_PARAM", f"Invalid {name}") from exc


@router.get("/v3/publicRooms")
async def get_public_rooms(
    request: Request,
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    return await rooms.public_rooms(
        limit=_int_param(request.query_params.get("limit"), "limit"),
        since=request.query_params.get("since"),
    )


@router.post("/v3/publicRooms")
async def post_public_rooms(
    request: Request,
    rooms: RoomService = Depends(get_rooms),
) -> dict[str, Any]:
    body = await json_body(request, strict=False)
    raw_limit = body.get("limit")
    limit = int(raw_limit) if isinstance(raw_limit, int) else None
    since = body.get("since") if isinstance(body.get("since"), str) else None
    term = None
    filt = body.get("filter")
    if isinstance(filt, dict) and isinstance(filt.get("generic_search_term"), str):
        term = filt["generic_search_term"]
    return await rooms.public_rooms(term=term, limit=limit, since=since)
