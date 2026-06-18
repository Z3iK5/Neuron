# SPDX-License-Identifier: Apache-2.0
"""Federation read API (``/_matrix/federation/v1/...``) — HS-7.

Serves the signed PDUs this server produces so a remote homeserver can fetch
events and room state. Requests are authenticated with the ``X-Matrix`` scheme.

Honest scope: the origin server's verify keys are resolved **locally only** for
now (we can authenticate our own server — useful for loopback testing — but
fetching a *remote* server's keys needs the outbound federation client, a later
step). Room state is served as the room's **current** state; per-event historical
state (state groups) is also deferred.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

from neuron_server.errors import MatrixError
from neuron_server.federation.auth import parse_authorization_header, verify_request
from neuron_server.storage import rooms as store

router = APIRouter(prefix="/_matrix/federation/v1")

_SERVER_NAME = "Neuron"
_SERVER_VERSION = "0.0.1"


def _domain_of(user_id: str) -> str:
    return user_id.split(":", 1)[1] if ":" in user_id else ""


async def _require_origin(request: Request) -> str:
    """Authenticate an inbound federation request, returning the origin server."""
    creds = parse_authorization_header(request.headers.get("Authorization", ""))
    if creds is None:
        raise MatrixError(401, "M_UNAUTHORIZED", "Missing or malformed X-Matrix authorization")

    settings = request.app.state.settings
    server_keys = request.app.state.server_keys
    if creds.origin == settings.name:
        verify_keys = {kid: v["key"] for kid, v in server_keys.verify_keys().items()}
    else:
        # Remote key resolution arrives with the outbound federation client.
        raise MatrixError(
            401, "M_UNAUTHORIZED", f"Cannot resolve keys for remote origin {creds.origin!r} yet"
        )

    uri = request.url.path
    if request.url.query:
        uri += "?" + request.url.query
    if not verify_request(
        creds,
        method=request.method,
        uri=uri,
        destination=settings.name,
        verify_keys=verify_keys,
    ):
        raise MatrixError(403, "M_FORBIDDEN", "Federation request signature did not verify")
    return creds.origin


async def _require_origin_in_room(request: Request, room_id: str) -> str:
    origin = await _require_origin(request)
    members = await store.get_joined_members(request.app.state.db, room_id)
    if not any(_domain_of(uid) == origin for uid in members):
        raise MatrixError(403, "M_FORBIDDEN", "Origin server is not in the room")
    return origin


@router.get("/version")
async def federation_version() -> dict[str, Any]:
    # Unauthenticated by spec.
    return {"server": {"name": _SERVER_NAME, "version": _SERVER_VERSION}}


@router.get("/event/{event_id}")
async def get_event(event_id: str, request: Request) -> dict[str, Any]:
    await _require_origin(request)
    db = request.app.state.db
    event = await store.get_event_global(db, event_id)
    if event is None:
        raise MatrixError(404, "M_NOT_FOUND", "Unknown event")
    await _require_origin_in_room(request, event.room_id)
    return {
        "origin": request.app.state.settings.name,
        "origin_server_ts": int(time.time() * 1000),
        "pdus": [event.pdu_dict()],
    }


async def _current_state_and_auth_chain(
    request: Request, room_id: str
) -> tuple[list[Any], list[Any]]:
    db = request.app.state.db
    if await store.get_room(db, room_id) is None:
        raise MatrixError(404, "M_NOT_FOUND", "Unknown room")
    event_id = request.query_params.get("event_id")
    if event_id is not None and await store.get_event(db, room_id, event_id) is None:
        raise MatrixError(404, "M_NOT_FOUND", "Unknown event for room")
    state = await store.get_current_state(db, room_id)
    auth_seed: list[str] = []
    for event in state:
        auth_seed.extend(event.auth_events)
    auth_chain = await store.get_auth_chain(db, room_id, auth_seed)
    return state, auth_chain


@router.get("/state/{room_id}")
async def get_state(room_id: str, request: Request) -> dict[str, Any]:
    await _require_origin_in_room(request, room_id)
    state, auth_chain = await _current_state_and_auth_chain(request, room_id)
    return {
        "pdus": [event.pdu_dict() for event in state],
        "auth_chain": [event.pdu_dict() for event in auth_chain],
    }


@router.get("/state_ids/{room_id}")
async def get_state_ids(room_id: str, request: Request) -> dict[str, Any]:
    await _require_origin_in_room(request, room_id)
    state, auth_chain = await _current_state_and_auth_chain(request, room_id)
    return {
        "pdu_ids": [event.event_id for event in state],
        "auth_chain_ids": [event.event_id for event in auth_chain],
    }
