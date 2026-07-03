# SPDX-License-Identifier: Apache-2.0
"""Federated room joins — resident side (HS-7 step 6a).

Lets a *remote* server join one of its users to a room **we** host:

* ``GET  /_matrix/federation/v1/make_join/{roomId}/{userId}`` — return an unsigned
  join-event template (auth/prev events, depth) for the remote server to complete.
* ``PUT  /_matrix/federation/v2/send_join/{roomId}/{eventId}`` — accept the remote
  server's signed join event, authorise and persist it, and return the room's
  current state and auth chain so the joining server can adopt the room.

The joining (outbound) side — building the join from the template and storing the
returned room state locally — is the next sub-step.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from neuron_server.api.deps import json_body
from neuron_server.errors import MatrixError
from neuron_server.federation.request import authenticate_request
from neuron_server.federation.validation import domain_of, validate_pdu
from neuron_server.rooms import versions
from neuron_server.rooms.service import RoomService

router = APIRouter(prefix="/_matrix/federation")


def _rooms(request: Request) -> RoomService:
    service: RoomService = request.app.state.rooms
    return service


@router.get("/v1/make_join/{room_id}/{user_id}")
async def make_join(room_id: str, user_id: str, request: Request) -> dict[str, Any]:
    origin = await authenticate_request(request)
    if domain_of(user_id) != origin:
        raise MatrixError(403, "M_FORBIDDEN", "Cannot make_join for a user on another server")

    requested_versions = request.query_params.getlist("ver")
    if requested_versions and not any(versions.is_supported(v) for v in requested_versions):
        raise MatrixError(
            400,
            "M_INCOMPATIBLE_ROOM_VERSION",
            "None of the requested room versions are supported",
        )
    return await _rooms(request).make_join_template(room_id, user_id)


@router.put("/v2/send_join/{room_id}/{event_id}")
async def send_join(room_id: str, event_id: str, request: Request) -> dict[str, Any]:
    pdu = await json_body(request, message="Join event must be a JSON object")

    origin = await authenticate_request(request, content=pdu)
    resolver = request.app.state.server_key_resolver
    await validate_pdu(pdu, resolver=resolver)
    if domain_of(str(pdu.get("sender", ""))) != origin:
        raise MatrixError(403, "M_FORBIDDEN", "Join event sender is not on the origin server")

    state, auth_chain = await _rooms(request).apply_external_join(room_id, pdu)
    return {
        "origin": request.app.state.settings.name,
        "state": [event.pdu_dict() for event in state],
        "auth_chain": [event.pdu_dict() for event in auth_chain],
        "event": pdu,
    }
