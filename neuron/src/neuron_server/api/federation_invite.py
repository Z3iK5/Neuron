# SPDX-License-Identifier: Apache-2.0
"""Federated invites — invited-user's-server side (HS-7 step 6e).

When a room hosted elsewhere invites one of *our* users, the resident server pushes
the invite event here. We validate it, **co-sign** it, record the invite, and
return the doubly-signed event.

``PUT /_matrix/federation/v2/invite/{roomId}/{eventId}``

Honest scope: the recorded invite is not yet surfaced in this server's ``/sync``
(that needs out-of-band invite state in the sync builder); the invite is stored and
acknowledged, which is what lets the invited user then join over federation.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from neuron_server.api.deps import json_body
from neuron_server.crypto.event_hashing import add_signature
from neuron_server.errors import MatrixError
from neuron_server.federation.request import authenticate_request
from neuron_server.federation.validation import domain_of, validate_pdu
from neuron_server.storage import invites as invite_store

router = APIRouter(prefix="/_matrix/federation")


@router.put("/v2/invite/{room_id}/{event_id}")
async def receive_invite(room_id: str, event_id: str, request: Request) -> dict[str, Any]:
    body = await json_body(request, message="Invite body must be a JSON object")

    origin = await authenticate_request(request, content=body)
    event = body.get("event")
    if not isinstance(event, dict):
        raise MatrixError(400, "M_BAD_JSON", "Missing invite event")

    await validate_pdu(event, resolver=request.app.state.server_key_resolver)
    if (event.get("content") or {}).get("membership") != "invite":
        raise MatrixError(400, "M_INVALID_PARAM", "Event is not an invite")

    settings = request.app.state.settings
    invited_user = str(event.get("state_key", ""))
    if domain_of(invited_user) != settings.name:
        raise MatrixError(403, "M_FORBIDDEN", "Invited user is not on this server")
    if domain_of(str(event.get("sender", ""))) != origin:
        raise MatrixError(403, "M_FORBIDDEN", "Invite sender is not on the origin server")

    # Co-sign the invite and record it for the local user.
    signed = add_signature(
        event,
        server_name=settings.name,
        signing_key=request.app.state.server_keys.signing_key,
    )
    invite_state = body.get("invite_room_state")
    await invite_store.store_invite(
        request.app.state.db,
        invited_user,
        room_id,
        signed,
        invite_state if isinstance(invite_state, list) else [],
    )
    request.app.state.notify()  # wake any long-polling /sync for the invited user
    return {"event": signed}
