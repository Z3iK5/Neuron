# SPDX-License-Identifier: Apache-2.0
"""Federation query API (``GET /_matrix/federation/v1/query/profile``) — HS-7.

Serves local users' public profile (displayname / avatar_url) to other
homeservers, so remote member lists can show our users properly. Requests are
authenticated with the ``X-Matrix`` scheme like every other federation route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from neuron_server.errors import MatrixError
from neuron_server.federation.request import authenticate_request
from neuron_server.federation.validation import domain_of
from neuron_server.storage import accounts, userdata

router = APIRouter(prefix="/_matrix/federation/v1")

_PROFILE_FIELDS = ("displayname", "avatar_url")


@router.get("/query/profile")
async def query_profile(request: Request) -> dict[str, Any]:
    await authenticate_request(request)

    user_id = request.query_params.get("user_id") or ""
    field = request.query_params.get("field")
    if field is not None and field not in _PROFILE_FIELDS:
        raise MatrixError(400, "M_INVALID_PARAM", "Unknown profile field")

    db = request.app.state.db
    # Per spec we only answer for our own users; anything else is M_NOT_FOUND.
    if domain_of(user_id) != request.app.state.settings.name:
        raise MatrixError(404, "M_NOT_FOUND", "User is not on this server")
    if await accounts.get_user(db, user_id) is None:
        raise MatrixError(404, "M_NOT_FOUND", "Unknown user")

    profile = await userdata.get_profile(db, user_id)
    if field is not None:
        # Only the requested field, and only when it is set.
        return {field: profile[field]} if field in profile else {}
    return profile
