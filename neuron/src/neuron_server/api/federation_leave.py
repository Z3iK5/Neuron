# SPDX-License-Identifier: Apache-2.0
"""Federated leaves — resident side (HS-7 step 6d).

Mirrors make_join/send_join so a remote server can take one of its users out of a
room **we** host (a real leave, or rejecting an invite):

* ``GET /_matrix/federation/v1/make_leave/{roomId}/{userId}`` — a leave-event
  template for the remote server to complete and sign.
* ``PUT /_matrix/federation/v2/send_leave/{roomId}/{eventId}`` — accept and apply
  the signed leave event.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from neuron_server.api.deps import json_body
from neuron_server.errors import MatrixError
from neuron_server.federation.request import authenticate_request
from neuron_server.federation.validation import domain_of, validate_pdu
from neuron_server.rooms.service import RoomService

router = APIRouter(prefix="/_matrix/federation")


def _rooms(request: Request) -> RoomService:
    service: RoomService = request.app.state.rooms
    return service


@router.get("/v1/make_leave/{room_id}/{user_id}")
async def make_leave(room_id: str, user_id: str, request: Request) -> dict[str, Any]:
    origin = await authenticate_request(request)
    if domain_of(user_id) != origin:
        raise MatrixError(403, "M_FORBIDDEN", "Cannot make_leave for a user on another server")
    return await _rooms(request).make_leave_template(room_id, user_id)


@router.put("/v2/send_leave/{room_id}/{event_id}")
async def send_leave(room_id: str, event_id: str, request: Request) -> dict[str, Any]:
    pdu = await json_body(request, message="Leave event must be a JSON object")

    origin = await authenticate_request(request, content=pdu)
    await validate_pdu(pdu, resolver=request.app.state.server_key_resolver)
    if domain_of(str(pdu.get("sender", ""))) != origin:
        raise MatrixError(403, "M_FORBIDDEN", "Leave event sender is not on the origin server")

    await _rooms(request).apply_external_leave(room_id, pdu)
    return {}
