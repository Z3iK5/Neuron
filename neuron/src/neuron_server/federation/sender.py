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
        self, room_id: str, *, pdus: list[dict[str, Any]], edus: list[dict[str, Any]]
    ) -> None:
        destinations = await self.remote_destinations(room_id)
        for server in destinations:
            await self._deliver(server, new_pdus=pdus, edus=edus)

    async def _deliver(
        self,
        server: str,
        *,
        new_pdus: list[dict[str, Any]],
        edus: list[dict[str, Any]],
    ) -> None:
        """Send any queued PDUs for ``server`` plus the new ones; on failure, the
        new PDUs are queued for a later retry (EDUs are best-effort, not queued)."""
        pending = await outbox_store.get_pending(self._db, server)
        all_pdus = [pdu for _, pdu in pending] + new_pdus
        if not all_pdus and not edus:
            return
        transaction = {
            "origin": self._server_name,
            "origin_server_ts": int(time.time() * 1000),
            "pdus": all_pdus,
            "edus": edus,
        }
        txn_id = secrets.token_urlsafe(8)
        try:
            await self._client.put_json(
                server, f"/_matrix/federation/v1/send/{txn_id}", transaction
            )
        except Exception as exc:  # best effort; never block the local action
            _logger.warning("failed to send transaction to %s: %s", server, exc)
            for pdu in new_pdus:
                await outbox_store.enqueue(self._db, server, pdu)
            return
        if pending:
            await outbox_store.delete(self._db, [stream_id for stream_id, _ in pending])

    async def retry(self, server: str) -> None:
        """Attempt to flush any queued events for one destination server."""
        await self._deliver(server, new_pdus=[], edus=[])

    async def retry_all(self) -> None:
        """Attempt to flush queued events for every destination with a backlog."""
        for server in await outbox_store.destinations_with_pending(self._db):
            await self.retry(server)

    async def send_event(self, room_id: str, pdu: dict[str, Any]) -> None:
        await self._send_transaction(room_id, pdus=[pdu], edus=[])

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
