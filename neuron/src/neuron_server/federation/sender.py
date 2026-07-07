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
            await self._deliver(server, new_pdus=pdus, transient_edus=edus)

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
        transient_edus: list[dict[str, Any]],
    ) -> None:
        """Send ``server``'s queued PDUs and queued (durable) EDUs — both claimed
        under a lease so no other worker double-sends them — plus the new PDUs and
        any ``transient_edus``, in order, split into sequential transactions of at
        most 50 PDUs / 100 EDUs each (the spec's limits).

        Delivered claimed rows are deleted batch-by-batch; the first failed batch
        stops the send (preserving per-destination ordering), releases the claimed
        remainder (PDUs and EDUs) for immediate retry, and queues the still-unsent
        new PDUs behind it. ``transient_edus`` are best-effort (typing/receipts):
        sent if the destination is reachable now, dropped on failure — never queued."""
        owner = secrets.token_hex(8)
        now = int(time.time() * 1000)
        claimed = await outbox_store.claim_pending(
            self._db, server, owner, now_ms=now, lease_until_ms=now + _LEASE_MS
        )
        claimed_edus = await outbox_store.claim_pending_edus(
            self._db, server, owner, now_ms=now, lease_until_ms=now + _LEASE_MS
        )
        # (stream_id, unit) for the claimed backlog; (None, unit) for the new units,
        # which sort behind it (they'd get higher stream ids if queued). Transient
        # EDUs carry no stream id and are simply dropped if their batch fails.
        pdu_items: list[tuple[int | None, dict[str, Any]]] = [
            *claimed, *((None, pdu) for pdu in new_pdus)
        ]
        edu_items: list[tuple[int | None, dict[str, Any]]] = [
            *claimed_edus, *((None, edu) for edu in transient_edus)
        ]
        if not pdu_items and not edu_items:
            return
        p_index = 0  # first undelivered PDU
        e_index = 0  # first undelivered EDU
        try:
            while p_index < len(pdu_items) or e_index < len(edu_items):
                pdu_batch = pdu_items[p_index : p_index + _MAX_PDUS_PER_TXN]
                edu_batch = edu_items[e_index : e_index + _MAX_EDUS_PER_TXN]
                if not await self._send_now(
                    server,
                    pdus=[pdu for _, pdu in pdu_batch],
                    edus=[edu for _, edu in edu_batch],
                ):
                    break
                # Delete delivered rows per batch, so a crash between batches never
                # re-sends what the destination has already accepted.
                await outbox_store.delete(
                    self._db, [sid for sid, _ in pdu_batch if sid is not None], owner
                )
                await outbox_store.delete_edus(
                    self._db, [sid for sid, _ in edu_batch if sid is not None], owner
                )
                p_index += len(pdu_batch)
                e_index += len(edu_batch)
            else:
                return  # everything delivered
        except BaseException:
            # Cancellation (e.g. shutdown) or any error mid-send: hand the leases back
            # immediately so the backlog isn't stuck until the lease expires.
            await self._release_remainder(server, pdu_items, edu_items, p_index, e_index, owner)
            raise
        # Failure: hand the claimed remainder back for immediate retry (in order),
        # and queue the unsent new PDUs behind it.
        await self._release_remainder(server, pdu_items, edu_items, p_index, e_index, owner)
        for sid, pdu in pdu_items[p_index:]:
            if sid is None:
                await outbox_store.enqueue(self._db, server, pdu)

    async def _release_remainder(
        self,
        server: str,
        pdu_items: list[tuple[int | None, dict[str, Any]]],
        edu_items: list[tuple[int | None, dict[str, Any]]],
        p_index: int,
        e_index: int,
        owner: str,
    ) -> None:
        """Release the still-leased PDU and EDU rows from the undelivered remainder."""
        await outbox_store.release(
            self._db, [sid for sid, _ in pdu_items[p_index:] if sid is not None], owner
        )
        await outbox_store.release_edus(
            self._db, [sid for sid, _ in edu_items[e_index:] if sid is not None], owner
        )

    async def retry(self, server: str) -> None:
        """Attempt to flush any queued PDUs and EDUs for one destination server."""
        await self._deliver(server, new_pdus=[], transient_edus=[])

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
        ``m.direct_to_device`` EDU. Durably queued (not best-effort): a dropped Olm
        message means the recipient can't decrypt, so it must survive an offline
        peer and be retried. The payloads are opaque (Olm-encrypted) and never
        logged; the recipient dedups on ``message_id`` so a retry applies once."""
        edu = {
            "edu_type": "m.direct_to_device",
            "content": {
                "sender": sender,
                "type": event_type,
                "message_id": message_id,
                "messages": messages,
            },
        }
        await outbox_store.enqueue_edu(self._db, destination, edu)
        await self._deliver(destination, new_pdus=[], transient_edus=[])

    async def send_device_list_update(
        self, user_id: str, device_id: str, stream_id: int, deleted: bool = False
    ) -> None:
        """Tell every server sharing a room with the local ``user_id`` that their
        device set changed, so remote clients re-query the keys. Durably queued: a
        dropped update leaves remote users with a stale key set, so it must survive
        an offline peer (re-querying keys is idempotent, so redelivery is harmless)."""
        content: dict[str, Any] = {
            "user_id": user_id,
            "device_id": device_id,
            "stream_id": stream_id,
        }
        if deleted:
            content["deleted"] = True
        edu = {"edu_type": "m.device_list_update", "content": content}
        for server in await self.user_remote_servers(user_id):
            await outbox_store.enqueue_edu(self._db, server, edu)
            await self._deliver(server, new_pdus=[], transient_edus=[])
