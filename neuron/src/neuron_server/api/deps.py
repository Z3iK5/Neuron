# SPDX-License-Identifier: Apache-2.0
"""Shared FastAPI dependencies for the Client-Server API routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request

from neuron_server.auth.service import Authenticated, AuthService
from neuron_server.errors import MatrixError
from neuron_server.rooms.service import RoomService
from neuron_server.storage import accounts


def get_auth(request: Request) -> AuthService:
    """Return the per-app :class:`AuthService` (built during lifespan)."""
    service: AuthService = request.app.state.auth
    return service


def get_rooms(request: Request) -> RoomService:
    """Return the per-app :class:`RoomService` (built during lifespan)."""
    service: RoomService = request.app.state.rooms
    return service


async def json_body(
    request: Request,
    *,
    message: str = "Request body must be a JSON object",
    strict: bool = True,
) -> dict[str, Any]:
    """Parse the JSON request body, or raise the spec's M_NOT_JSON error.

    A syntactically valid body that is not a JSON object raises M_BAD_JSON with
    ``message``; with ``strict=False`` it is treated as an empty body instead.
    """
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MatrixError(400, "M_NOT_JSON", "Request body is not valid JSON") from exc
    if isinstance(data, dict):
        return data
    if strict:
        raise MatrixError(400, "M_BAD_JSON", message)
    return {}


def require_target_user(body: dict[str, Any]) -> str:
    """Return the required ``user_id`` from a parsed body, or raise M_MISSING_PARAM."""
    target = body.get("user_id")
    if not isinstance(target, str):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing user_id")
    return target


def _extract_token(request: Request) -> str | None:
    """Pull the access token from the ``Authorization`` header or query param."""
    header = request.headers.get("Authorization")
    if header and header.startswith("Bearer "):
        return header[len("Bearer ") :]
    return request.query_params.get("access_token")


async def require_user(request: Request) -> Authenticated:
    """Require and resolve a valid access token, or raise the spec error."""
    token = _extract_token(request)
    if not token:
        raise MatrixError(401, "M_MISSING_TOKEN", "Missing access token")
    auth = get_auth(request)
    who = await auth.lookup_token(token)
    if who is not None:
        return who
    # A known-but-expired token is a soft logout: tell the client so it silently
    # refreshes instead of hard-logging-out. A genuinely unknown token is not.
    if await auth.token_is_expired(token):
        raise MatrixError(
            401,
            "M_UNKNOWN_TOKEN",
            "Access token has expired",
            extra={"soft_logout": True},
        )
    raise MatrixError(401, "M_UNKNOWN_TOKEN", "Invalid access token")


async def require_admin(request: Request) -> Authenticated:
    """Require a valid token whose user is a server admin (config list or DB flag)."""
    who = await require_user(request)
    if who.user_id in request.app.state.settings.admin_user_ids():
        return who
    row = await accounts.get_user(request.app.state.db, who.user_id)
    if row is not None and row.admin:
        return who
    raise MatrixError(403, "M_FORBIDDEN", "You are not a server admin")
