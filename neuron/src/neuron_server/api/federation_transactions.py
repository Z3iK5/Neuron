# SPDX-License-Identifier: Apache-2.0
"""Inbound federation transactions (``PUT /_matrix/federation/v1/send/{txnId}``) — HS-7.

Receives a transaction of PDUs from another server, authenticates the request
(X-Matrix, over the signed body), and validates each PDU cryptographically,
returning the spec's per-PDU result map.

Honest scope: an accepted PDU here means it is **cryptographically valid** (well
formed, content hash intact, signed by the sender's server). Durable state
application — authorising the event against its ``auth_events`` and resolving room
state — is the next step (state resolution v2); this endpoint does not yet persist
received events into room state.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request

from neuron_server.errors import MatrixError
from neuron_server.federation.request import authenticate_request
from neuron_server.federation.validation import (
    PduValidationError,
    best_effort_event_id,
    validate_pdu,
)

router = APIRouter(prefix="/_matrix/federation/v1")

# A defensive cap on how many PDUs/EDUs one transaction may carry (the spec's
# recommended maximum is 50 each).
_MAX_PDUS = 50


@router.put("/send/{txn_id}")
async def send_transaction(txn_id: str, request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError as exc:
        raise MatrixError(400, "M_NOT_JSON", "Request body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise MatrixError(400, "M_BAD_JSON", "Transaction must be a JSON object")

    origin = await authenticate_request(request, content=body)
    body_origin = body.get("origin")
    if body_origin is not None and body_origin != origin:
        raise MatrixError(403, "M_FORBIDDEN", "Transaction origin does not match the signature")

    pdus = body.get("pdus") or []
    if not isinstance(pdus, list) or len(pdus) > _MAX_PDUS:
        raise MatrixError(400, "M_INVALID_PARAM", "Invalid or oversized pdus list")

    resolver = request.app.state.server_key_resolver
    results: dict[str, dict[str, Any]] = {}
    for pdu in pdus:
        try:
            event_id = await validate_pdu(pdu, resolver=resolver)
            results[event_id] = {}
        except PduValidationError as exc:
            results[best_effort_event_id(pdu)] = {"error": exc.reason}
    return {"pdus": results}
