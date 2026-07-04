# SPDX-License-Identifier: Apache-2.0
"""Outbound event propagation (HS-7 step 6g).

When an event is added to a room, the resident server sends it on to every other
server that has a joined member, in a transaction to their
``/_matrix/federation/v1/send/{txnId}`` endpoint. Failures are best-effort and
logged — they must not break the local send.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from neuron_core import get_logger
from neuron_server.federation.client import FederationClient
from neuron_server.federation.validation import domain_of
from neuron_server.storage import outbox as outbox_store
from neuron_server.storage import rooms as store
from neuron_server.storage.database import Database

_logger = get_logger(__name__)

# How long a worker's claim on a destination's outbox rows lasts. The send is quick
# (rows are deleted on success or released on failure right after); this only bounds
# how long a crashed worker's in-flight batch waits before another worker retries it.
_LEASE_MS = 5 * 60 * 1000

# The spec's transaction limits: at most 50 PDUs and 100 EDUs per transaction.
# A larger backlog is split into sequential transactions in stream order.
_MAX_PDUS_PER_TXN = 50
_MAX_EDUS_PER_TXN = 100


class FederationSender:
    """Sends locally-created events to the rooms' remote participants."""

    def __init__(self, db: Database, server_name: str, client: FederationClient) -> None:
        self._db = db
        self._server_name = server_name
        self._client = client

    async def remote_destinations(self, room_id: str) -> set[str]:
        """The set of other servers with a joined member in ``room_id``."""
        members = await store.get_joined_members(self._db, room_id)
        return {
            domain_of(user_id)
            for user_id in members
            if domain_of(user_id) != self._server_name
        }

    async def _send_transaction(
        self,
        room_id: str,
        *,
        pdus: list[dict[str, Any]],
        edus: list[dict[str, Any]],
        extra_destinations: set[str] | None = None,
        exclude: set[str] | None = None,
    ) -> None:
        destinations = await self.remote_destinations(room_id)
        if extra_destinations:
            # A membership removal (kick/ban/leave) drops the affected server from
            # the room's joined members, so the caller passes a pre-change snapshot
            # to make sure that server still receives the event.
            destinations |= {d for d in extra_destinations if d != self._server_name}
        if exclude:
            # Hub fanout: a relayed PDU must never go back to the server it came
            # from (or to ourselves) — that is what keeps relaying loop-free.
            destinations -= exclude
        for server in destinations:
            await self._deliver(server, new_pdus=pdus, edus=edus)

    async def _send_now(
        self, server: str, *, pdus: list[dict[str, Any]], edus: list[dict[str, Any]]
    ) -> bool:
        """Send one transaction to ``server``; return whether it succeeded."""
        if not pdus and not edus:
            return True
        transaction = {
            "origin": self._server_name,
            "origin_server_ts": int(time.time() * 1000),
            "pdus": pdus,
            "edus": edus,
        }
        txn_id = secrets.token_urlsafe(8)
        try:
            await self._client.put_json(
                server, f"/_matrix/federation/v1/send/{txn_id}", transaction
            )
        except Exception as exc:  # best effort; never block the local action
            _logger.warning("failed to send transaction to %s: %s", server, exc)
            return False
        return True

    async def _deliver(
        self,
        server: str,
        *,
        new_pdus: list[dict[str, Any]],
        edus: list[dict[str, Any]],
    ) -> None:
        """Send ``server``'s queued PDUs (claimed under a lease so no other worker
        double-sends them) plus the new ones, in order, split into sequential
        transactions of at most 50 PDUs / 100 EDUs each (the spec's limits).

        Delivered claimed rows are deleted batch-by-batch; the first failed batch
        stops the send (preserving per-destination ordering), releases the claimed
        remainder for immediate retry, and queues the still-unsent new PDUs behind
        it. EDUs are best-effort and never queued."""
        owner = secrets.token_hex(8)
        now = int(time.time() * 1000)
        claimed = await outbox_store.claim_pending(
            self._db, server, owner, now_ms=now, lease_until_ms=now + _LEASE_MS
        )
        # (stream_id, pdu) for the claimed backlog; (None, pdu) for the new PDUs,
        # which sort behind it (they'd get higher stream ids if queued).
        items: list[tuple[int | None, dict[str, Any]]] = [
            *claimed, *((None, pdu) for pdu in new_pdus)
        ]
        pending_edus = list(edus)
        if not items and not pending_edus:
            return
        index = 0  # first undelivered item
        try:
            while index < len(items) or pending_edus:
                batch = items[index : index + _MAX_PDUS_PER_TXN]
                batch_edus = pending_edus[:_MAX_EDUS_PER_TXN]
                if not await self._send_now(
                    server, pdus=[pdu for _, pdu in batch], edus=batch_edus
                ):
                    break
                # Delete delivered rows per batch, so a crash between batches never
                # re-sends what the destination has already accepted.
                await outbox_store.delete(
                    self._db, [sid for sid, _ in batch if sid is not None], owner
                )
                index += len(batch)
                pending_edus = pending_edus[len(batch_edus):]
            else:
                return  # everything delivered
        except BaseException:
            # Cancellation (e.g. shutdown) or any error mid-send: hand the lease back
            # immediately so the backlog isn't stuck until the lease expires.
            await outbox_store.release(
                self._db, [sid for sid, _ in items[index:] if sid is not None], owner
            )
            raise
        # Failure: hand the claimed remainder back for immediate retry (in order),
        # and queue the unsent new PDUs behind it.
        await outbox_store.release(
            self._db, [sid for sid, _ in items[index:] if sid is not None], owner
        )
        for sid, pdu in items[index:]:
            if sid is None:
                await outbox_store.enqueue(self._db, server, pdu)

    async def retry(self, server: str) -> None:
        """Attempt to flush any queued events for one destination server."""
        await self._deliver(server, new_pdus=[], edus=[])

    async def retry_all(self) -> None:
        """Attempt to flush queued events for every destination with a backlog."""
        now = int(time.time() * 1000)
        for server in await outbox_store.destinations_with_pending(self._db, now):
            await self.retry(server)

    async def send_event(
        self,
        room_id: str,
        pdu: dict[str, Any],
        *,
        extra_destinations: set[str] | None = None,
        exclude: set[str] | None = None,
    ) -> None:
        await self._send_transaction(
            room_id, pdus=[pdu], edus=[], extra_destinations=extra_destinations,
            exclude=exclude,
        )

    async def send_receipt(
        self, room_id: str, user_id: str, receipt_type: str, event_id: str, ts: int
    ) -> None:
        edu = {
            "edu_type": "m.receipt",
            "content": {
                room_id: {
                    receipt_type: {
                        user_id: {"data": {"ts": ts}, "event_ids": [event_id]}
                    }
                }
            },
        }
        await self._send_transaction(room_id, pdus=[], edus=[edu])

    async def send_typing(self, room_id: str, user_id: str, typing: bool) -> None:
        edu = {
            "edu_type": "m.typing",
            "content": {"room_id": room_id, "user_id": user_id, "typing": typing},
        }
        await self._send_transaction(room_id, pdus=[], edus=[edu])

    # --- E2EE over federation ------------------------------------------------

    async def user_remote_servers(self, user_id: str) -> set[str]:
        """Every other server sharing at least one joined room with ``user_id``."""
        sharing = await store.get_users_sharing_room(self._db, user_id)
        return {domain_of(u) for u in sharing} - {self._server_name}

    async def send_direct_to_device(
        self,
        destination: str,
        *,
        sender: str,
        event_type: str,
        message_id: str,
        messages: dict[str, Any],
    ) -> None:
        """Send to-device messages for ``destination``'s users as one
        ``m.direct_to_device`` EDU. Best-effort, like the other EDUs — the message
        payloads are opaque (Olm-encrypted) and never logged."""
        edu = {
            "edu_type": "m.direct_to_device",
            "content": {
                "sender": sender,
                "type": event_type,
                "message_id": message_id,
                "messages": messages,
            },
        }
        await self._deliver(destination, new_pdus=[], edus=[edu])

    async def send_device_list_update(
        self, user_id: str, device_id: str, stream_id: int, deleted: bool = False
    ) -> None:
        """Tell every server sharing a room with the local ``user_id`` that their
        device set changed, so remote clients re-query the keys."""
        content: dict[str, Any] = {
            "user_id": user_id,
            "device_id": device_id,
            "stream_id": stream_id,
        }
        if deleted:
            content["deleted"] = True
        edu = {"edu_type": "m.device_list_update", "content": content}
        for server in await self.user_remote_servers(user_id):
            await self._deliver(server, new_pdus=[], edus=[edu])
