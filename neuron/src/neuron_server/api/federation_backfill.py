# SPDX-License-Identifier: Apache-2.0
"""Federation backfill (HS-7 step 6h).

``GET /_matrix/federation/v1/backfill/{roomId}?v=<eventId>&limit=<n>`` returns a
transaction of events going *backwards* in the room DAG from the given event(s), so
a server that has just joined (or that is missing history) can fetch a room's recent
timeline. Authenticated, and only for servers that are in the room.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

from neuron_server.errors import MatrixError
from neuron_server.federation.request import authenticate_request
from neuron_server.federation.validation import domain_of
from neuron_server.storage import rooms as store

router = APIRouter(prefix="/_matrix/federation/v1")

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 100


@router.get("/backfill/{room_id}")
async def backfill(room_id: str, request: Request) -> dict[str, Any]:
    origin = await authenticate_request(request)
    db = request.app.state.db
    if await store.get_room(db, room_id) is None:
        raise MatrixError(404, "M_NOT_FOUND", "Unknown room")
    members = await store.get_joined_members(db, room_id)
    if not any(domain_of(uid) == origin for uid in members):
        raise MatrixError(403, "M_FORBIDDEN", "Origin server is not in the room")

    try:
        limit = min(int(request.query_params.get("limit", _DEFAULT_LIMIT)), _MAX_LIMIT)
    except ValueError:
        limit = _DEFAULT_LIMIT

    # Start from the earliest of the requester's known events; if none are known to
    # us, start from the most recent event.
    orderings: list[int] = []
    for event_id in request.query_params.getlist("v"):
        event = await store.get_event(db, room_id, event_id)
        if event is not None:
            orderings.append(event.stream_ordering)
    cutoff = min(orderings) if orderings else (await store.next_stream_ordering(db))

    events = await store.get_messages(
        db, room_id, from_ordering=cutoff, direction="b", limit=limit
    )
    return {
        "origin": request.app.state.settings.name,
        "origin_server_ts": int(time.time() * 1000),
        "pdus": [event.pdu_dict() for event in events],
    }
