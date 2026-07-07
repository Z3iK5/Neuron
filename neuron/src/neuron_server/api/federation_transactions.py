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

import time
from typing import Any

from fastapi import APIRouter, Request

from neuron_server.api.deps import json_body
from neuron_server.errors import MatrixError
from neuron_server.federation.request import authenticate_request
from neuron_server.federation.validation import (
    PduValidationError,
    best_effort_event_id,
    domain_of,
    validate_pdu,
)
from neuron_server.storage import e2ee as e2ee_store
from neuron_server.storage import receipts as receipts_store
from neuron_server.storage import rooms as store
from neuron_server.storage import transactions as txn_store

router = APIRouter(prefix="/_matrix/federation/v1")

# Defensive caps on how many PDUs/EDUs one transaction may carry (the spec's
# maximum: 50 PDUs and 100 EDUs).
_MAX_PDUS = 50
_MAX_EDUS = 100


@router.put("/send/{txn_id}")
async def send_transaction(txn_id: str, request: Request) -> dict[str, Any]:
    body = await json_body(request, message="Transaction must be a JSON object")

    origin = await authenticate_request(request, content=body)
    body_origin = body.get("origin")
    if body_origin is not None and body_origin != origin:
        raise MatrixError(403, "M_FORBIDDEN", "Transaction origin does not match the signature")

    pdus = body.get("pdus") or []
    if not isinstance(pdus, list) or len(pdus) > _MAX_PDUS:
        raise MatrixError(400, "M_INVALID_PARAM", "Invalid or oversized pdus list")

    db = request.app.state.db
    # A retry of an already-processed transaction (possibly to a different worker)
    # is a no-op — return success without re-validating/re-applying it.
    if await txn_store.was_received(db, origin, txn_id):
        return {"pdus": {}}

    resolver = request.app.state.server_key_resolver
    rooms = request.app.state.rooms
    results: dict[str, dict[str, Any]] = {}
    for pdu in pdus:
        try:
            event_id = await validate_pdu(pdu, resolver=resolver)
            # Apply the event to our copy of the room (no-op if we don't have it).
            await rooms.apply_remote_event(pdu)
            results[event_id] = {}
        except PduValidationError as exc:
            results[best_effort_event_id(pdu)] = {"error": exc.reason}

    edus = body.get("edus") or []
    if not isinstance(edus, list) or len(edus) > _MAX_EDUS:
        raise MatrixError(400, "M_INVALID_PARAM", "Invalid or oversized edus list")
    if edus:
        await _process_edus(request, origin, edus)
    # Record it so a later retry short-circuits. After processing, so a crash
    # mid-transaction leaves it un-recorded and the retry reprocesses it. Reprocessing
    # is safe: event application skips already-stored events, and the EDUs here
    # (receipts, typing) are current-state upserts — a reprocess just refreshes them
    # (at worst an extra /sync notification for an ephemeral value).
    await txn_store.mark_received(db, origin, txn_id, int(time.time() * 1000))
    return {"pdus": results}


async def _process_edus(request: Request, origin: str, edus: list[Any]) -> None:
    """Apply ephemeral data units (receipts, typing, E2EE) from a transaction."""
    db = request.app.state.db
    server_name = request.app.state.settings.name
    touched = False
    for edu in edus:
        if not isinstance(edu, dict):
            continue
        edu_type = edu.get("edu_type")
        content = edu.get("content")
        if not isinstance(content, dict):
            continue
        if edu_type == "m.direct_to_device":
            # To-device messages (e.g. Olm-encrypted room keys) for local users.
            # The sender must belong to the origin server; only local recipients
            # are stored (device_id "*" fans out inside send_to_device). Content
            # is opaque key material — never logged.
            sender = content.get("sender")
            event_type = content.get("type")
            messages = content.get("messages")
            message_id = content.get("message_id")
            if (
                isinstance(sender, str)
                and domain_of(sender) == origin
                and isinstance(event_type, str)
                and isinstance(messages, dict)
                and isinstance(message_id, str)
            ):
                # Durable retry redelivers in a fresh transaction, so transaction
                # dedup can't catch a re-sent Olm message; dedup on (origin,
                # message_id) makes storing it exactly-once. message_id is opaque.
                if await e2ee_store.was_to_device_seen(db, origin, message_id):
                    continue
                local = {
                    user_id: by_device
                    for user_id, by_device in messages.items()
                    if domain_of(user_id) == server_name
                }
                if local:
                    # Wakes /sync itself once stored.
                    await request.app.state.e2ee.send_to_device(sender, event_type, local)
                await e2ee_store.mark_to_device_seen(
                    db, origin, message_id, int(time.time() * 1000)
                )
            continue
        if edu_type == "m.device_list_update":
            # A remote user's device set changed. Family-scale approach: record
            # the change on the shared device-list stream so local users sharing
            # an encrypted room see the user in /sync device_lists.changed and
            # re-fetch their keys — /keys/query always goes to the remote server
            # live, so no remote device cache/resync machinery is needed.
            user_id = content.get("user_id")
            if isinstance(user_id, str) and domain_of(user_id) == origin:
                await e2ee_store.bump_device_list(db, user_id)
                touched = True
            continue
        if edu_type == "m.typing":
            room_id = content.get("room_id")
            user_id = content.get("user_id")
            if isinstance(room_id, str) and isinstance(user_id, str):
                if await store.get_room(db, room_id) is not None:
                    await request.app.state.typing.set_typing(
                        room_id, user_id, bool(content.get("typing"))
                    )
            continue
        if edu_type != "m.receipt":
            continue
        for room_id, by_type in content.items():
            if await store.get_room(db, room_id) is None or not isinstance(by_type, dict):
                continue
            for receipt_type, users in by_type.items():
                if not isinstance(users, dict):
                    continue
                for user_id, info in users.items():
                    event_ids = info.get("event_ids") if isinstance(info, dict) else None
                    ts = int((info.get("data") or {}).get("ts", 0)) if isinstance(info, dict) else 0
                    for event_id in event_ids or []:
                        await receipts_store.upsert_receipt(
                            db, room_id, user_id, receipt_type, event_id, ts
                        )
                        touched = True
    if touched:
        request.app.state.notify()
