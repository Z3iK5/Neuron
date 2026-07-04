# SPDX-License-Identifier: Apache-2.0
"""Client-Server API: E2EE key distribution & to-device (HS-5).

Device-key upload/query, one-time-key claim, cross-signing upload, signature
upload, and ``sendToDevice``. The server stores and relays this material but never
decrypts it.

Requests naming *remote* users are federated: ``/keys/query`` and ``/keys/claim``
are proxied to each remote user's homeserver (an unreachable destination is
reported under ``failures``, per the spec, without failing the request), and
``sendToDevice`` messages for remote users are bundled into ``m.direct_to_device``
EDUs.

HS-5 scope: cross-signing upload here does **not** enforce UIA (the spec usually
requires it); server-side key backup (``/room_keys``) is a separate follow-up.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from neuron_server.api.deps import json_body, require_user
from neuron_server.auth.service import Authenticated
from neuron_server.e2ee.service import E2EEService
from neuron_server.federation.client import FederationClient
from neuron_server.federation.sender import FederationSender
from neuron_server.federation.validation import domain_of

router = APIRouter(prefix="/_matrix/client")

# How long a remote /keys/query result is served from memory. Element re-queries
# keys frequently (on every room open / member change), so a short TTL keeps that
# cheap without holding stale keys long: a device change also arrives as an
# m.device_list_update EDU, which makes clients re-query after the TTL anyway.
# Caveat: the cache is per-process — with multiple workers each keeps its own
# copy, so a query may hit a cold worker. That only costs an extra remote fetch.
_REMOTE_KEYS_TTL_S = 60.0

# (destination, canonical request body) -> (expires_at_monotonic, response)
_RemoteKeysCache = dict[tuple[str, str], tuple[float, dict[str, Any]]]


def get_e2ee(request: Request) -> E2EEService:
    service: E2EEService = request.app.state.e2ee
    return service


def _split_by_server(
    request: Request, requested: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Split a user-keyed map into local users and per-remote-server batches."""
    server_name = request.app.state.settings.name
    local: dict[str, Any] = {}
    remote: dict[str, dict[str, Any]] = {}
    for user_id, value in requested.items():
        destination = domain_of(user_id)
        if destination == server_name:
            local[user_id] = value
        else:
            remote.setdefault(destination, {})[user_id] = value
    return local, remote


def _remote_keys_cache(request: Request) -> _RemoteKeysCache:
    cache: _RemoteKeysCache | None = getattr(request.app.state, "remote_keys_cache", None)
    if cache is None:
        cache = {}
        request.app.state.remote_keys_cache = cache
    return cache


async def _query_remote_keys(
    request: Request, destination: str, users: dict[str, Any]
) -> dict[str, Any]:
    """Fetch ``users``' keys from ``destination`` (with a short in-process cache)."""
    cache = _remote_keys_cache(request)
    now = time.monotonic()
    # Prune expired entries so the cache can't grow without bound.
    for key in [k for k, (expires, _) in cache.items() if expires <= now]:
        del cache[key]
    cache_key = (destination, json.dumps(users, sort_keys=True))
    hit = cache.get(cache_key)
    if hit is not None:
        return hit[1]

    client: FederationClient = request.app.state.federation_client
    response = await client.post_json(
        destination, "/_matrix/federation/v1/user/keys/query", {"device_keys": users}
    )
    cache[cache_key] = (now + _REMOTE_KEYS_TTL_S, response)
    return response


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
    requested = body.get("device_keys") or {}
    local, remote = _split_by_server(request, requested)

    result = await e2ee.query_keys({"device_keys": local})
    for destination, users in remote.items():
        try:
            remote_result = await _query_remote_keys(request, destination, users)
        except Exception:
            # Per the CS spec, an unreachable homeserver is reported under
            # "failures" and must not fail the whole request.
            result["failures"][destination] = {}
            continue
        for section in ("device_keys", "master_keys", "self_signing_keys"):
            fetched = remote_result.get(section)
            if isinstance(fetched, dict):
                result[section].update(fetched)
    return result


@router.post("/v3/keys/claim")
async def keys_claim(
    request: Request,
    who: Authenticated = Depends(require_user),
    e2ee: E2EEService = Depends(get_e2ee),
) -> dict[str, Any]:
    body = await json_body(request)
    requested = body.get("one_time_keys") or {}
    local, remote = _split_by_server(request, requested)

    result = await e2ee.claim_keys({"one_time_keys": local})
    client: FederationClient = request.app.state.federation_client
    for destination, users in remote.items():
        try:
            remote_result = await client.post_json(
                destination,
                "/_matrix/federation/v1/user/keys/claim",
                {"one_time_keys": users},
            )
        except Exception:
            result["failures"][destination] = {}
            continue
        claimed = remote_result.get("one_time_keys")
        if isinstance(claimed, dict):
            result["one_time_keys"].update(claimed)
    return result


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
        local, remote = _split_by_server(request, messages)
        if local:
            await e2ee.send_to_device(who.user_id, event_type, local)
        sender: FederationSender = request.app.state.federation_sender
        for destination, batch in remote.items():
            # Best-effort, like the other EDUs; the payloads are Olm-encrypted
            # and opaque to us either way.
            await sender.send_direct_to_device(
                destination,
                sender=who.user_id,
                event_type=event_type,
                message_id=txn_id,
                messages=batch,
            )
    return {}
