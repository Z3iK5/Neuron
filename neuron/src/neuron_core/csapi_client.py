# SPDX-License-Identifier: Apache-2.0
"""A minimal **Matrix Client-Server API** client used by Neuron bots.

The Admin API (``AdminClient``) lets a server admin manage the server, but
some moderation actions must be performed *as a room member* — e.g. kicking or
banning a user. Those use the standard Client-Server API, authenticated as the
bot's own account.

This client is intentionally small and **does not do end-to-end encryption** —
Phase 3 targets unencrypted rooms. E2EE-capable bots come later (Phase 5) using a
crypto-aware library.
"""

from __future__ import annotations

from typing import Any

from neuron_core._http import BaseApiClient
from neuron_core.errors import MatrixError


class MatrixClient(BaseApiClient):
    """Typed async client for the bits of the Client-Server API a bot needs."""

    _error_cls = MatrixError

    # --- identity -----------------------------------------------------------
    async def whoami(self) -> dict[str, Any]:
        """``GET /_matrix/client/v3/account/whoami`` — who this token belongs to."""
        return await self._request("GET", "/_matrix/client/v3/account/whoami")

    async def joined_rooms(self) -> list[str]:
        """``GET /_matrix/client/v3/joined_rooms`` — room IDs this account is in."""
        body = await self._request("GET", "/_matrix/client/v3/joined_rooms")
        rooms: list[str] = body.get("joined_rooms", [])
        return rooms

    # --- syncing & membership ----------------------------------------------
    async def sync(
        self,
        *,
        since: str | None = None,
        timeout_ms: int = 30000,
        filter_id: str | None = None,
    ) -> dict[str, Any]:
        """``GET /_matrix/client/v3/sync`` — long-poll for new events.

        Pass the previous response's ``next_batch`` as ``since`` to get only what
        happened since. With no ``since`` this returns the current state (an
        initial sync).
        """
        params: dict[str, Any] = {"timeout": timeout_ms}
        if since is not None:
            params["since"] = since
        if filter_id is not None:
            params["filter"] = filter_id
        return await self._request("GET", "/_matrix/client/v3/sync", params=params)

    async def join_room(self, room_id_or_alias: str) -> dict[str, Any]:
        """``POST /_matrix/client/v3/join/{roomIdOrAlias}`` — join a room."""
        return await self._request("POST", f"/_matrix/client/v3/join/{room_id_or_alias}")

    async def keys_upload(
        self,
        *,
        device_keys: dict[str, Any] | None = None,
        one_time_keys: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /_matrix/client/v3/keys/upload`` — publish device + one-time keys.

        Publishing the device's identity keys and a supply of one-time keys is what
        lets other devices claim a key and send this bot Olm-encrypted room keys.
        Returns ``{"one_time_key_counts": {...}}``.
        """
        body: dict[str, Any] = {}
        if device_keys is not None:
            body["device_keys"] = device_keys
        if one_time_keys is not None:
            body["one_time_keys"] = one_time_keys
        return await self._request("POST", "/_matrix/client/v3/keys/upload", json=body)

    async def upload_cross_signing_keys(
        self, payload: dict[str, Any], *, auth: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """``POST /_matrix/client/v3/keys/device_signing/upload`` — publish cross-signing keys.

        Note: the homeserver usually requires user-interactive auth (UIA) for this,
        supplied via ``auth``; without it the server replies 401 with a UIA flow.
        """
        body = dict(payload)
        if auth is not None:
            body["auth"] = auth
        return await self._request(
            "POST", "/_matrix/client/v3/keys/device_signing/upload", json=body
        )

    async def upload_signatures(self, signatures: dict[str, Any]) -> dict[str, Any]:
        """``POST /_matrix/client/v3/keys/signatures/upload`` — publish key signatures."""
        return await self._request(
            "POST", "/_matrix/client/v3/keys/signatures/upload", json=signatures
        )

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
