"""A minimal **Matrix Client-Server API** client used by Neuron bots.

The Admin API (``SynapseAdminClient``) lets a server admin manage the server, but
some moderation actions must be performed *as a room member* — e.g. kicking or
banning a user, or redacting a specific message. Those use the standard
Client-Server API, authenticated as the bot's own account.

This client is intentionally small and **does not do end-to-end encryption** —
Phase 3 targets unencrypted rooms. E2EE-capable bots come later (Phase 5) using a
crypto-aware library.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any

import httpx

from neuron_core._http import ok_json
from neuron_core.errors import MatrixError


class MatrixClient:
    """Typed async client for the bits of the Client-Server API a bot needs."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> MatrixClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._client.request(method, path, json=json)
        return ok_json(response, MatrixError)

    # --- identity -----------------------------------------------------------
    async def whoami(self) -> dict[str, Any]:
        """``GET /_matrix/client/v3/account/whoami`` — who this token belongs to."""
        return await self._request("GET", "/_matrix/client/v3/account/whoami")

    async def joined_rooms(self) -> list[str]:
        """``GET /_matrix/client/v3/joined_rooms`` — room IDs this account is in."""
        body = await self._request("GET", "/_matrix/client/v3/joined_rooms")
        rooms: list[str] = body.get("joined_rooms", [])
        return rooms

    # --- membership moderation ---------------------------------------------
    async def kick(
        self, room_id: str, user_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """``POST /_matrix/client/v3/rooms/{roomId}/kick`` — remove a user from a room."""
        body: dict[str, Any] = {"user_id": user_id}
        if reason:
            body["reason"] = reason
        return await self._request("POST", f"/_matrix/client/v3/rooms/{room_id}/kick", json=body)

    async def ban(self, room_id: str, user_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """``POST /_matrix/client/v3/rooms/{roomId}/ban`` — ban a user from a room."""
        body: dict[str, Any] = {"user_id": user_id}
        if reason:
            body["reason"] = reason
        return await self._request("POST", f"/_matrix/client/v3/rooms/{room_id}/ban", json=body)

    # --- content moderation -------------------------------------------------
    async def redact_event(
        self, room_id: str, event_id: str, *, reason: str | None = None
    ) -> str:
        """``PUT /_matrix/client/v3/rooms/{roomId}/redact/{eventId}/{txnId}``.

        Redacts a single event. Returns the new redaction event's ID.
        """
        txn_id = f"neuron-{int(time.time() * 1000)}"
        body: dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        result = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/redact/{event_id}/{txn_id}",
            json=body,
        )
        event: str = result.get("event_id", "")
        return event

    # --- power levels -------------------------------------------------------
    async def get_power_levels(self, room_id: str) -> dict[str, Any]:
        """Read the ``m.room.power_levels`` state event for a room."""
        return await self._request(
            "GET", f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/"
        )

    async def set_power_levels(self, room_id: str, content: dict[str, Any]) -> dict[str, Any]:
        """Write a new ``m.room.power_levels`` state event for a room."""
        return await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels/",
            json=content,
        )
