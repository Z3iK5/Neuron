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

from collections.abc import Awaitable, Callable
from typing import Any

from neuron_core import get_logger
from neuron_server.crypto.event_hashing import add_hashes_and_signatures, compute_event_id
from neuron_server.crypto.signing import SigningKey
from neuron_server.errors import MatrixError
from neuron_server.federation.client import FederationClient
from neuron_server.federation.validation import KeyResolver, PduValidationError, validate_pdu
from neuron_server.rooms import versions
from neuron_server.rooms.events import Event
from neuron_server.storage import invites as invite_store
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
        apply_event: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
    ) -> None:
        self._db = db
        self._server_name = server_name
        self._signing_key = signing_key
        self._client = client
        self._resolver = resolver
        self._notify = notify
        self._apply_event = apply_event

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
        # The room is now joined locally, so any pending invite is consumed.
        await invite_store.delete_invite(self._db, user_id, room_id)
        # Pull in recent history so the new member isn't staring at an empty room.
        await self._backfill(server, room_id, event_id)
        if self._notify is not None:
            self._notify()
        return room_id

    async def _backfill(
        self, server: str, room_id: str, from_event_id: str, limit: int = 20
    ) -> None:
        if self._apply_event is None:
            return
        try:
            response = await self._client.get_json(
                server,
                f"/_matrix/federation/v1/backfill/{room_id}?v={from_event_id}&limit={limit}",
            )
        except Exception as exc:  # backfill is best effort
            _logger.warning("backfill from %s failed: %s", server, exc)
            return
        pdus = [pdu for pdu in response.get("pdus", []) if isinstance(pdu, dict)]
        # The transaction lists events newest-first; apply oldest-first.
        for pdu in reversed(pdus):
            try:
                await validate_pdu(pdu, resolver=self._resolver)
            except PduValidationError:
                continue
            await self._apply_event(pdu)

    async def leave(self, room_id: str, user_id: str, via: list[str]) -> str:
        """Leave a remote ``room_id`` (via one of the ``via`` servers)."""
        candidates = via or [_domain_of(room_id)]
        last_error: Exception | None = None
        for server in candidates:
            if server == self._server_name:
                continue
            try:
                return await self._leave_via(server, room_id, user_id)
            except Exception as exc:
                _logger.warning("federated leave via %s failed: %s", server, exc)
                last_error = exc
        raise MatrixError(
            502, "M_UNKNOWN", f"Could not leave {room_id} over federation: {last_error}"
        )

    async def _leave_via(self, server: str, room_id: str, user_id: str) -> str:
        make = await self._client.get_json(
            server, f"/_matrix/federation/v1/make_leave/{room_id}/{user_id}"
        )
        template = make.get("event")
        if not isinstance(template, dict):
            raise MatrixError(502, "M_UNKNOWN", "make_leave did not return an event template")

        template = dict(template)
        template["content"] = {"membership": "leave"}
        signed = add_hashes_and_signatures(
            template, server_name=self._server_name, signing_key=self._signing_key
        )
        event_id = compute_event_id(signed)
        await self._client.put_json(
            server, f"/_matrix/federation/v2/send_leave/{room_id}/{event_id}", signed
        )

        # Reflect the leave in our local copy of the room, if we have one, and
        # clear any pending invite (this also handles rejecting an invite).
        if await store.get_room(self._db, room_id) is not None:
            await self._apply_local_leave(room_id, signed, event_id)
        await invite_store.delete_invite(self._db, user_id, room_id)
        if self._notify is not None:
            self._notify()
        return room_id

    async def _apply_local_leave(
        self, room_id: str, pdu: dict[str, Any], event_id: str
    ) -> None:
        async with self._db.transaction():
            if await store.get_event(self._db, room_id, event_id) is None:
                stream = await store.next_stream_ordering(self._db)
                await store.insert_event(self._db, Event.from_pdu(pdu, event_id, stream))
            await store.update_current_state(
                self._db, room_id, "m.room.member", str(pdu["state_key"]), event_id
            )
            await store.set_membership(self._db, room_id, str(pdu["state_key"]), "leave")

    async def send_invite(
        self,
        server: str,
        room_id: str,
        pdu: dict[str, Any],
        invite_state: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Push an invite event to the invited user's ``server``; return its
        co-signed form (falling back to our event if the peer omits it)."""
        event_id = compute_event_id(pdu)
        body = {"event": pdu, "room_version": "11", "invite_room_state": invite_state}
        response = await self._client.put_json(
            server, f"/_matrix/federation/v2/invite/{room_id}/{event_id}", body
        )
        returned = response.get("event")
        return returned if isinstance(returned, dict) else pdu

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
