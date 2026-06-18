# SPDX-License-Identifier: Apache-2.0
"""Outbound federated membership — joining a *remote* room (HS-7 step 6b).

When one of our users joins a room hosted elsewhere, we run the joining side of
the make_join/send_join handshake and then **persist the returned room state
locally**, so the room becomes a normal local room our user is joined to.

Honest scope: the returned state is adopted directly (the resident server is
trusted to have resolved it). Conflict resolution across multiple resident servers
needs state resolution v2, which is the next sub-step.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from neuron_core import get_logger
from neuron_server.crypto.event_hashing import add_hashes_and_signatures, compute_event_id
from neuron_server.crypto.signing import SigningKey
from neuron_server.errors import MatrixError
from neuron_server.federation.client import FederationClient
from neuron_server.federation.validation import KeyResolver, PduValidationError, validate_pdu
from neuron_server.rooms import versions
from neuron_server.rooms.events import Event
from neuron_server.storage import rooms as store
from neuron_server.storage.database import Database

_logger = get_logger(__name__)


def _domain_of(identifier: str) -> str:
    return identifier.split(":", 1)[1] if ":" in identifier else identifier


class FederatedMembership:
    """Joins our users to rooms hosted on other servers."""

    def __init__(
        self,
        db: Database,
        server_name: str,
        signing_key: SigningKey,
        client: FederationClient,
        resolver: KeyResolver,
        notify: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._server_name = server_name
        self._signing_key = signing_key
        self._client = client
        self._resolver = resolver
        self._notify = notify

    async def join(self, room_id: str, user_id: str, via: list[str]) -> str:
        """Join ``user_id`` to a remote ``room_id`` via one of the ``via`` servers."""
        candidates = via or [_domain_of(room_id)]
        last_error: Exception | None = None
        for server in candidates:
            if server == self._server_name:
                continue
            try:
                return await self._join_via(server, room_id, user_id)
            except Exception as exc:  # try the next resident server
                _logger.warning("federated join via %s failed: %s", server, exc)
                last_error = exc
        raise MatrixError(
            502, "M_UNKNOWN", f"Could not join {room_id} over federation: {last_error}"
        )

    async def _join_via(self, server: str, room_id: str, user_id: str) -> str:
        make = await self._client.get_json(
            server, f"/_matrix/federation/v1/make_join/{room_id}/{user_id}?ver=11"
        )
        room_version = str(make.get("room_version", versions.DEFAULT_ROOM_VERSION))
        template = make.get("event")
        if not isinstance(template, dict):
            raise MatrixError(502, "M_UNKNOWN", "make_join did not return an event template")

        template = dict(template)
        template["content"] = {"membership": "join"}
        signed = add_hashes_and_signatures(
            template, server_name=self._server_name, signing_key=self._signing_key
        )
        event_id = compute_event_id(signed)

        response = await self._client.put_json(
            server, f"/_matrix/federation/v2/send_join/{room_id}/{event_id}", signed
        )
        state = [p for p in response.get("state", []) if isinstance(p, dict)]
        auth_chain = [p for p in response.get("auth_chain", []) if isinstance(p, dict)]

        # Trust nothing unverified: every returned event must be a valid PDU.
        for pdu in (*state, *auth_chain):
            try:
                await validate_pdu(pdu, resolver=self._resolver, room_version=room_version)
            except PduValidationError as exc:
                raise MatrixError(
                    502, "M_UNKNOWN", f"resident server returned an invalid event: {exc.reason}"
                ) from exc

        await self._store_room(room_id, room_version, state, auth_chain, signed)
        if self._notify is not None:
            self._notify()
        return room_id

    async def _store_room(
        self,
        room_id: str,
        room_version: str,
        state: list[dict[str, Any]],
        auth_chain: list[dict[str, Any]],
        join_event: dict[str, Any],
    ) -> None:
        events: dict[str, dict[str, Any]] = {}
        for pdu in (*auth_chain, *state, join_event):
            events[compute_event_id(pdu)] = pdu

        create = next(
            (p for p in state if p.get("type") == "m.room.create" and p.get("state_key") == ""),
            None,
        )
        if create is None:
            raise MatrixError(502, "M_UNKNOWN", "send_join state has no create event")

        async with self._db.transaction():
            if await store.get_room(self._db, room_id) is None:
                await store.create_room_row(
                    self._db,
                    room_id,
                    str(create["sender"]),
                    room_version,
                    int(create["origin_server_ts"]),
                )
            # Insert events parents-first (ascending depth) so the DAG reads sanely.
            for event_id, pdu in sorted(events.items(), key=lambda kv: int(kv[1].get("depth", 0))):
                if await store.get_event(self._db, room_id, event_id) is not None:
                    continue
                stream = await store.next_stream_ordering(self._db)
                await store.insert_event(self._db, Event.from_pdu(pdu, event_id, stream))
            # Adopt the returned state as our current state.
            for pdu in state:
                state_key = pdu.get("state_key")
                if state_key is None:
                    continue
                event_id = compute_event_id(pdu)
                await store.update_current_state(
                    self._db, room_id, str(pdu["type"]), str(state_key), event_id
                )
                if pdu["type"] == "m.room.member":
                    await store.set_membership(
                        self._db,
                        room_id,
                        str(state_key),
                        str((pdu.get("content") or {}).get("membership")),
                    )
