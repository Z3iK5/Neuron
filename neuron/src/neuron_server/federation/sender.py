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
    ) -> None:
        destinations = await self.remote_destinations(room_id)
        if extra_destinations:
            # A membership removal (kick/ban/leave) drops the affected server from
            # the room's joined members, so the caller passes a pre-change snapshot
            # to make sure that server still receives the event.
            destinations |= {d for d in extra_destinations if d != self._server_name}
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
        double-sends them) plus the new ones, in order. On success the claimed rows
        are deleted; on failure they're released (immediate retry) and the new PDUs
        queued. EDUs are best-effort and never queued."""
        owner = secrets.token_hex(8)
        now = int(time.time() * 1000)
        claimed = await outbox_store.claim_pending(
            self._db, server, owner, now_ms=now, lease_until_ms=now + _LEASE_MS
        )
        claimed_ids = [stream_id for stream_id, _ in claimed]
        all_pdus = [pdu for _, pdu in claimed] + new_pdus
        if not all_pdus and not edus:
            return
        try:
            sent = await self._send_now(server, pdus=all_pdus, edus=edus)
        except BaseException:
            # Cancellation (e.g. shutdown) or any error mid-send: hand the lease back
            # immediately so the backlog isn't stuck until the lease expires.
            await outbox_store.release(self._db, claimed_ids, owner)
            raise
        if sent:
            await outbox_store.delete(self._db, claimed_ids, owner)
            return
        # Failure: hand the claimed backlog back for immediate retry, queue the new.
        await outbox_store.release(self._db, claimed_ids, owner)
        for pdu in new_pdus:
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
        self, room_id: str, pdu: dict[str, Any], *, extra_destinations: set[str] | None = None
    ) -> None:
        await self._send_transaction(
            room_id, pdus=[pdu], edus=[], extra_destinations=extra_destinations
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
