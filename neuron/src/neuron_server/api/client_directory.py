# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: user directory search and VoIP (TURN) credentials.

The user directory searches *local* accounts only (localpart + displayname);
remote-user discovery is out of scope for a single-family server. TURN
credentials follow coturn's REST scheme (``use-auth-secret``): a time-limited
``expiry:user_id`` username with a base64 HMAC-SHA1 password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server.api.deps import json_body, require_user
from neuron_server.auth.service import Authenticated
from neuron_server.clock import now_ms
from neuron_server.errors import MatrixError
from neuron_server.storage import accounts

router = APIRouter(prefix="/_matrix/client")


@router.post("/v3/user_directory/search")
async def user_directory_search(
    request: Request,
    who: Authenticated = Depends(require_user),
) -> dict[str, Any]:
    body = await json_body(request)
    search_term = body.get("search_term")
    if not isinstance(search_term, str) or not search_term:
        raise MatrixError(400, "M_MISSING_PARAM", "Missing search_term")
    raw_limit = body.get("limit", 10)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise MatrixError(400, "M_INVALID_PARAM", "limit must be an integer") from exc
    limit = max(1, min(limit, 1000))

    # Fetch one extra row to learn whether the results were truncated.
    rows = await accounts.search_users(
        request.app.state.db, search_term, request.app.state.settings.name, limit + 1
    )
    limited = len(rows) > limit
    return {
        "results": [
            {"user_id": user_id, "display_name": display_name, "avatar_url": avatar_url}
            for user_id, display_name, avatar_url in rows[:limit]
        ],
        "limited": limited,
    }


@router.get("/v3/voip/turnServer")
async def turn_server(
    request: Request,
    who: Authenticated = Depends(require_user),
) -> dict[str, Any]:
    settings = request.app.state.settings
    secret = (
        settings.turn_shared_secret.get_secret_value()
        if settings.turn_shared_secret is not None
        else ""
    )
    if not settings.turn_uris or not secret:
        # Element treats empty uris as "no TURN server configured".
        return {"uris": [], "username": "", "password": "", "ttl": settings.turn_ttl_s}

    expiry_ts = now_ms() // 1000 + settings.turn_ttl_s
    username = f"{expiry_ts}:{who.user_id}"
    digest = hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
    return {
        "uris": list(settings.turn_uris),
        "username": username,
        "password": base64.b64encode(digest).decode("ascii"),
        "ttl": settings.turn_ttl_s,
    }
