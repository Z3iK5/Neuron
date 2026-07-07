# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: Simplified Sliding Sync (MSC4186).

``POST /_matrix/client/unstable/org.matrix.simplified_msc3575/sync`` — the sync
Element X and other modern mobile clients use. The same handler is also mounted at
the newer ``org.matrix.msc4186`` path. ``pos`` (the connection cursor) and
``timeout`` (long-poll milliseconds) may be passed as query params or in the body;
the JSON body carries the lists / room subscriptions / extensions request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server.api.deps import json_body, require_user
from neuron_server.auth.service import Authenticated
from neuron_server.errors import MatrixError
from neuron_server.sync.sliding import SlidingSyncService, parse_pos

router = APIRouter(prefix="/_matrix/client")

_PATHS = (
    "/unstable/org.matrix.simplified_msc3575/sync",
    "/unstable/org.matrix.msc4186/sync",
)


def get_sliding_sync(request: Request) -> SlidingSyncService:
    service: SlidingSyncService = request.app.state.sliding_sync
    return service


async def sliding_sync(
    request: Request,
    who: Authenticated = Depends(require_user),
    syncer: SlidingSyncService = Depends(get_sliding_sync),
) -> dict[str, Any]:
    body = await json_body(request, strict=False)
    # pos/timeout may arrive as query params (per the MSC) or inside the body.
    raw_pos = request.query_params.get("pos") or body.get("pos")
    pos = parse_pos(str(raw_pos) if raw_pos is not None else None)
    timeout_raw = request.query_params.get("timeout", body.get("timeout", 0))
    try:
        timeout_ms = int(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise MatrixError(400, "M_INVALID_PARAM", "timeout must be an integer") from exc
    return await syncer.sync(
        who.user_id,
        who.device_id,
        pos=pos,
        timeout_ms=max(0, timeout_ms),
        body=body,
    )


# The MSC's stable-unstable path plus the newer msc4186 alias share one handler.
for _path in _PATHS:
    router.add_api_route(_path, sliding_sync, methods=["POST"])
