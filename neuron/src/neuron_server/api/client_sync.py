# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: ``GET /sync`` (HS-3).

Long-polls for new events. The ``since`` token is the server-local stream
position; omit it for an initial sync. ``timeout`` (ms) controls how long an
incremental sync with no changes will wait before returning.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server.api.deps import require_user
from neuron_server.auth.service import Authenticated
from neuron_server.errors import MatrixError
from neuron_server.sync.service import SyncService

router = APIRouter(prefix="/_matrix/client")


def get_sync(request: Request) -> SyncService:
    service: SyncService = request.app.state.sync
    return service


@router.get("/v3/sync")
async def sync(
    request: Request,
    who: Authenticated = Depends(require_user),
    syncer: SyncService = Depends(get_sync),
) -> dict[str, Any]:
    timeout_param = request.query_params.get("timeout", "0")
    try:
        timeout_ms = int(timeout_param)
    except ValueError as exc:
        raise MatrixError(400, "M_INVALID_PARAM", "timeout must be an integer") from exc
    return await syncer.sync(
        who.user_id,
        who.device_id,
        since=request.query_params.get("since"),
        timeout_ms=max(0, timeout_ms),
    )
