# SPDX-License-Identifier: Apache-2.0
"""The Server-Server key API (``/_matrix/key/v2/...``) — HS-7.

Publishes this server's Ed25519 verify keys, self-signed, so other homeservers
can verify our signatures. This endpoint is unauthenticated by design (it is how
trust is bootstrapped). Key *querying* of remote servers (the notary
``/query`` endpoints) comes later in the federation epic.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from neuron_server.keys.service import ServerKeyService

router = APIRouter(prefix="/_matrix/key/v2")


def _keys(request: Request) -> ServerKeyService:
    service: ServerKeyService = request.app.state.server_keys
    return service


@router.get("/server")
async def get_server_keys(request: Request) -> dict[str, Any]:
    return _keys(request).server_key_document()


@router.get("/server/{key_id}")
async def get_server_keys_for_id(key_id: str, request: Request) -> dict[str, Any]:
    # The trailing key id is deprecated by the spec; the full document is returned
    # regardless of which key id was requested.
    return _keys(request).server_key_document()
