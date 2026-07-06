# SPDX-License-Identifier: Apache-2.0
"""Federation room-directory API (HS-7).

Serves two things to properly X-Matrix-signed peers:

- ``GET /_matrix/federation/v1/query/directory`` — resolve one of *our* local
  aliases to a room id and the servers that can service a join.
- ``GET``/``POST /_matrix/federation/v1/publicRooms`` — our public room directory,
  the same data the Client-Server ``/publicRooms`` endpoint returns.

Cross-server directory browsing is out of scope: we only ever answer with our own
rooms and our own aliases.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from neuron_server.api.deps import json_body
from neuron_server.errors import MatrixError
from neuron_server.federation.request import authenticate_request
from neuron_server.rooms.service import RoomService
from neuron_server.storage import directory as directory_store

router = APIRouter(prefix="/_matrix/federation/v1")


@router.get("/query/directory")
async def query_directory(request: Request) -> dict[str, Any]:
    await authenticate_request(request)
    alias = request.query_params.get("room_alias") or ""
    if not directory_store.is_valid_alias(alias):
        raise MatrixError(400, "M_INVALID_PARAM", "Invalid room alias")
    # We only answer for our own aliases; anything else is M_NOT_FOUND.
    if directory_store.alias_server(alias) != request.app.state.settings.name:
        raise MatrixError(404, "M_NOT_FOUND", "Room alias is not on this server")
    rooms: RoomService = request.app.state.rooms
    resolved = await rooms.resolve_local_alias(alias)
    if resolved is None:
        raise MatrixError(404, "M_NOT_FOUND", "Room alias not found")
    room_id, servers = resolved
    return {"room_id": room_id, "servers": servers}


@router.get("/publicRooms")
async def get_public_rooms(request: Request) -> dict[str, Any]:
    await authenticate_request(request)
    rooms: RoomService = request.app.state.rooms
    raw_limit = request.query_params.get("limit")
    limit = None
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise MatrixError(400, "M_INVALID_PARAM", "Invalid limit") from exc
    return await rooms.public_rooms(limit=limit, since=request.query_params.get("since"))


@router.post("/publicRooms")
async def post_public_rooms(request: Request) -> dict[str, Any]:
    body = await json_body(request, strict=False)
    await authenticate_request(request, content=body)
    rooms: RoomService = request.app.state.rooms
    raw_limit = body.get("limit")
    limit = int(raw_limit) if isinstance(raw_limit, int) else None
    since = body.get("since") if isinstance(body.get("since"), str) else None
    term = None
    filt = body.get("filter")
    if isinstance(filt, dict) and isinstance(filt.get("generic_search_term"), str):
        term = filt["generic_search_term"]
    return await rooms.public_rooms(term=term, limit=limit, since=since)
