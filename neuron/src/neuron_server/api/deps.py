# SPDX-License-Identifier: Apache-2.0
"""Shared FastAPI dependencies for the Client-Server API routes."""

from __future__ import annotations

from fastapi import Request

from neuron_server.auth.service import Authenticated, AuthService
from neuron_server.errors import MatrixError
from neuron_server.storage import accounts


def get_auth(request: Request) -> AuthService:
    """Return the per-app :class:`AuthService` (built during lifespan)."""
    service: AuthService = request.app.state.auth
    return service


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
    who = await get_auth(request).lookup_token(token)
    if who is None:
        raise MatrixError(401, "M_UNKNOWN_TOKEN", "Invalid access token")
    return who


async def require_admin(request: Request) -> Authenticated:
    """Require a valid token whose user is a server admin (config list or DB flag)."""
    who = await require_user(request)
    if who.user_id in request.app.state.settings.admin_user_ids():
        return who
    row = await accounts.get_user(request.app.state.db, who.user_id)
    if row is not None and row.admin:
        return who
    raise MatrixError(403, "M_FORBIDDEN", "You are not a server admin")
