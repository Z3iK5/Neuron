# SPDX-License-Identifier: Apache-2.0
"""Federation E2EE key endpoints.

Serves **local** users' E2EE key material to other homeservers so cross-server
encrypted rooms work:

- ``POST /user/keys/query`` — device keys + cross-signing keys,
- ``POST /user/keys/claim`` — one-time keys (consumed, race-safe),
- ``GET /user/devices/{userId}`` — a user's devices with their identity keys.

All requests are authenticated with the ``X-Matrix`` scheme. The key material is
opaque to the server and never logged.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from neuron_server.api.deps import json_body
from neuron_server.e2ee.service import E2EEService
from neuron_server.errors import MatrixError
from neuron_server.federation.request import authenticate_request
from neuron_server.federation.validation import domain_of
from neuron_server.storage import accounts
from neuron_server.storage import e2ee as e2ee_store

router = APIRouter(prefix="/_matrix/federation/v1")


def _local_only(request: Request, requested: dict[str, Any]) -> dict[str, Any]:
    """Keep only entries for users on this server — we never answer for others."""
    server_name = request.app.state.settings.name
    return {u: v for u, v in requested.items() if domain_of(u) == server_name}


@router.post("/user/keys/query")
async def user_keys_query(request: Request) -> dict[str, Any]:
    body = await json_body(request)
    await authenticate_request(request, content=body)

    e2ee: E2EEService = request.app.state.e2ee
    requested = body.get("device_keys")
    if not isinstance(requested, dict):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing device_keys")

    result = await e2ee.query_keys({"device_keys": _local_only(request, requested)})
    # The federation response omits user-signing keys — those are private to the
    # user's own homeserver (only their own clients may see them).
    return {
        "device_keys": result["device_keys"],
        "master_keys": result["master_keys"],
        "self_signing_keys": result["self_signing_keys"],
    }


@router.post("/user/keys/claim")
async def user_keys_claim(request: Request) -> dict[str, Any]:
    body = await json_body(request)
    await authenticate_request(request, content=body)

    e2ee: E2EEService = request.app.state.e2ee
    requested = body.get("one_time_keys")
    if not isinstance(requested, dict):
        raise MatrixError(400, "M_MISSING_PARAM", "Missing one_time_keys")

    result = await e2ee.claim_keys({"one_time_keys": _local_only(request, requested)})
    return {"one_time_keys": result["one_time_keys"]}


@router.get("/user/devices/{user_id}")
async def user_devices(user_id: str, request: Request) -> dict[str, Any]:
    await authenticate_request(request)

    db = request.app.state.db
    if domain_of(user_id) != request.app.state.settings.name:
        raise MatrixError(404, "M_NOT_FOUND", "User is not on this server")
    if await accounts.get_user(db, user_id) is None:
        raise MatrixError(404, "M_NOT_FOUND", "Unknown user")

    keys_by_device = await e2ee_store.get_device_keys_for_user(db, user_id)
    devices: list[dict[str, Any]] = []
    for device in await accounts.list_devices(db, user_id):
        keys = keys_by_device.get(device.device_id)
        if keys is None:
            continue  # a device without uploaded identity keys is invisible to E2EE
        entry: dict[str, Any] = {"device_id": device.device_id, "keys": keys}
        if device.display_name:
            entry["device_display_name"] = device.display_name
        devices.append(entry)

    response: dict[str, Any] = {
        "user_id": user_id,
        "stream_id": await db.get_stream_position("device_lists"),
        "devices": devices,
    }
    master = await e2ee_store.get_cross_signing_key(db, user_id, "master")
    if master is not None:
        response["master_key"] = master
    self_signing = await e2ee_store.get_cross_signing_key(db, user_id, "self_signing")
    if self_signing is not None:
        response["self_signing_key"] = self_signing
    return response
