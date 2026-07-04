# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: E2EE key distribution & to-device (HS-5).

Device-key upload/query, one-time-key claim, cross-signing upload, signature
upload, and ``sendToDevice``. The server stores and relays this material but never
decrypts it.

HS-5 scope: cross-signing upload here does **not** enforce UIA (the spec usually
requires it); server-side key backup (``/room_keys``) is a separate follow-up.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server.api.deps import json_body, require_user
from neuron_server.auth.service import Authenticated
from neuron_server.e2ee.service import E2EEService

router = APIRouter(prefix="/_matrix/client")


def get_e2ee(request: Request) -> E2EEService:
    service: E2EEService = request.app.state.e2ee
    return service


@router.post("/v3/keys/upload")
async def keys_upload(
    request: Request,
    who: Authenticated = Depends(require_user),
    e2ee: E2EEService = Depends(get_e2ee),
) -> dict[str, Any]:
    body = await json_body(request)
    return await e2ee.upload_keys(who.user_id, who.device_id, body)


@router.post("/v3/keys/query")
async def keys_query(
    request: Request,
    who: Authenticated = Depends(require_user),
    e2ee: E2EEService = Depends(get_e2ee),
) -> dict[str, Any]:
    body = await json_body(request)
    return await e2ee.query_keys(body)


@router.post("/v3/keys/claim")
async def keys_claim(
    request: Request,
    who: Authenticated = Depends(require_user),
    e2ee: E2EEService = Depends(get_e2ee),
) -> dict[str, Any]:
    body = await json_body(request)
    return await e2ee.claim_keys(body)


@router.post("/v3/keys/device_signing/upload")
async def device_signing_upload(
    request: Request,
    who: Authenticated = Depends(require_user),
    e2ee: E2EEService = Depends(get_e2ee),
) -> dict[str, Any]:
    body = await json_body(request)
    return await e2ee.upload_cross_signing_keys(who.user_id, body)


@router.post("/v3/keys/signatures/upload")
async def signatures_upload(
    request: Request,
    who: Authenticated = Depends(require_user),
    e2ee: E2EEService = Depends(get_e2ee),
) -> dict[str, Any]:
    body = await json_body(request)
    return await e2ee.upload_signatures(body)


@router.put("/v3/sendToDevice/{event_type}/{txn_id}")
async def send_to_device(
    event_type: str,
    txn_id: str,
    request: Request,
    who: Authenticated = Depends(require_user),
    e2ee: E2EEService = Depends(get_e2ee),
) -> dict[str, Any]:
    body = await json_body(request)
    messages = body.get("messages")
    if isinstance(messages, dict):
        await e2ee.send_to_device(who.user_id, event_type, messages)
    return {}
