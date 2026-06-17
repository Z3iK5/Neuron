# SPDX-License-Identifier: Apache-2.0
"""A small, typed client for the open **Synapse Admin API**.

The Synapse Admin API is documented in this repository under
``docs/admin_api/`` (e.g. ``user_admin_api.md``). It is a standard HTTP+JSON API
that requires a server-admin access token, sent as a Bearer token.

This client wraps the endpoints Neuron needs, one method per endpoint, with
typed inputs/outputs. Errors become :class:`SynapseAdminError`.

We use an *async* HTTP client (``httpx.AsyncClient``) because the services that
use this (FastAPI web apps, the Matrix bots) are themselves async.

Example::

    async with SynapseAdminClient("http://localhost:8008", token) as admin:
        version = await admin.get_server_version()
        page = await admin.list_users(limit=10)
        for user in page.users:
            print(user["name"])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import httpx

from neuron_core._http import ok_json
from neuron_core.errors import SynapseAdminError


@dataclass
class UserListPage:
    """One page of results from the "list users" admin endpoint.

    Synapse paginates users forward-only: if ``next_token`` is not ``None`` there
    are more results, and you pass it back as ``from_token`` to fetch the next page.
    """

    users: list[dict[str, Any]]
    total: int
    next_token: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class RoomListPage:
    """One page of results from the "list rooms" admin endpoint.

    Rooms paginate with a numeric offset: ``next_batch`` / ``prev_batch`` (when
    present) are the offsets to pass back as ``from_offset`` for the next/previous
    page.
    """

    rooms: list[dict[str, Any]]
    total_rooms: int
    offset: int = 0
    next_batch: int | None = None
    prev_batch: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class EventReportPage:
    """One page of results from the "event reports" admin endpoint."""

    event_reports: list[dict[str, Any]]
    total: int
    next_token: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class SynapseAdminClient:
    """Typed async client for the Synapse Admin API."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create the client.

        :param base_url: homeserver base URL, e.g. ``http://localhost:8008``.
        :param access_token: a server-admin access token.
        :param timeout: per-request timeout in seconds.
        :param client: an optional pre-built ``httpx.AsyncClient`` (used by tests
            to inject a mock transport). When omitted we build our own.
        """
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    # --- lifecycle ----------------------------------------------------------
    async def aclose(self) -> None:
        """Close the underlying HTTP client (only if we created it)."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> SynapseAdminClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # --- internal helpers ---------------------------------------------------
    @staticmethod
    def _ok_json(response: httpx.Response) -> dict[str, Any]:
        """Return the parsed JSON body, or raise ``SynapseAdminError`` on failure."""
        return ok_json(response, SynapseAdminError)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.request(method, path, params=params, json=json)
        return self._ok_json(response)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    # --- read endpoints -----------------------------------------------------
    async def get_server_version(self) -> dict[str, Any]:
        """``GET /_synapse/admin/v1/server_version`` — Synapse + Python versions.

        Returns a dict like ``{"server_version": "1.155.0", "python_version": "3.11.x"}``.
        Useful as a connectivity/health probe and for the console's dashboard.
        """
        return await self._get("/_synapse/admin/v1/server_version")

    async def list_users(
        self,
        *,
        from_token: str | None = None,
        limit: int = 100,
        name: str | None = None,
        guests: bool | None = None,
        deactivated: bool | None = None,
    ) -> UserListPage:
        """``GET /_synapse/admin/v2/users`` — list/search local user accounts."""
        params: dict[str, Any] = {"limit": limit}
        if from_token is not None:
            params["from"] = from_token
        if name is not None:
            params["name"] = name
        if guests is not None:
            params["guests"] = _bool_param(guests)
        if deactivated is not None:
            params["deactivated"] = _bool_param(deactivated)

        body = await self._get("/_synapse/admin/v2/users", params=params)
        return UserListPage(
            users=body.get("users", []),
            total=body.get("total", 0),
            next_token=body.get("next_token"),
            raw=body,
        )

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """``GET /_synapse/admin/v2/users/{user_id}`` — full details for one account."""
        return await self._get(f"/_synapse/admin/v2/users/{user_id}")

    async def list_rooms(
        self,
        *,
        from_offset: int = 0,
        limit: int = 100,
        search_term: str | None = None,
        order_by: str | None = None,
    ) -> RoomListPage:
        """``GET /_synapse/admin/v1/rooms`` — list/search rooms on the server."""
        params: dict[str, Any] = {"from": from_offset, "limit": limit}
        if search_term is not None:
            params["search_term"] = search_term
        if order_by is not None:
            params["order_by"] = order_by

        body = await self._get("/_synapse/admin/v1/rooms", params=params)
        return RoomListPage(
            rooms=body.get("rooms", []),
            total_rooms=body.get("total_rooms", 0),
            offset=body.get("offset", 0),
            next_batch=body.get("next_batch"),
            prev_batch=body.get("prev_batch"),
            raw=body,
        )

    async def get_room(self, room_id: str) -> dict[str, Any]:
        """``GET /_synapse/admin/v1/rooms/{room_id}`` — details for one room."""
        return await self._get(f"/_synapse/admin/v1/rooms/{room_id}")

    async def get_room_members(self, room_id: str) -> list[str]:
        """``GET /_synapse/admin/v1/rooms/{room_id}/members`` — member user IDs."""
        body = await self._get(f"/_synapse/admin/v1/rooms/{room_id}/members")
        members: list[str] = body.get("members", [])
        return members

    async def get_room_state(self, room_id: str) -> list[dict[str, Any]]:
        """``GET /_synapse/admin/v1/rooms/{room_id}/state`` — current room state."""
        body = await self._get(f"/_synapse/admin/v1/rooms/{room_id}/state")
        state: list[dict[str, Any]] = body.get("state", [])
        return state

    async def list_event_reports(
        self,
        *,
        from_offset: int = 0,
        limit: int = 100,
        user_id: str | None = None,
        room_id: str | None = None,
    ) -> EventReportPage:
        """``GET /_synapse/admin/v1/event_reports`` — user-submitted content reports."""
        params: dict[str, Any] = {"from": from_offset, "limit": limit}
        if user_id is not None:
            params["user_id"] = user_id
        if room_id is not None:
            params["room_id"] = room_id

        body = await self._get("/_synapse/admin/v1/event_reports", params=params)
        return EventReportPage(
            event_reports=body.get("event_reports", []),
            total=body.get("total", 0),
            next_token=body.get("next_token"),
            raw=body,
        )

    async def list_registration_tokens(self) -> list[dict[str, Any]]:
        """``GET /_synapse/admin/v1/registration_tokens`` — all registration tokens."""
        body = await self._get("/_synapse/admin/v1/registration_tokens")
        tokens: list[dict[str, Any]] = body.get("registration_tokens", [])
        return tokens

    # --- write endpoints ----------------------------------------------------
    async def upsert_user(
        self,
        user_id: str,
        *,
        password: str | None = None,
        displayname: str | None = None,
        admin: bool | None = None,
        deactivated: bool | None = None,
        locked: bool | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """``PUT /_synapse/admin/v2/users/{user_id}`` — create or modify an account.

        Returns ``(user, created)`` where ``created`` is ``True`` if a new account
        was created (HTTP 201) rather than an existing one modified (HTTP 200).
        Only the fields you pass are changed.
        """
        body: dict[str, Any] = {}
        if password is not None:
            body["password"] = password
        if displayname is not None:
            body["displayname"] = displayname
        if admin is not None:
            body["admin"] = admin
        if deactivated is not None:
            body["deactivated"] = deactivated
        if locked is not None:
            body["locked"] = locked

        response = await self._client.put(f"/_synapse/admin/v2/users/{user_id}", json=body)
        data = self._ok_json(response)
        return data, response.status_code == 201

    async def deactivate_user(self, user_id: str, *, erase: bool = False) -> dict[str, Any]:
        """``POST /_synapse/admin/v1/deactivate/{user_id}`` — deactivate an account.

        When ``erase`` is True the account is also GDPR-erased (removed from rooms,
        tokens/devices/3PIDs cleared). This is irreversible.
        """
        return await self._request(
            "POST", f"/_synapse/admin/v1/deactivate/{user_id}", json={"erase": erase}
        )

    async def reset_password(
        self, user_id: str, new_password: str, *, logout_devices: bool = True
    ) -> dict[str, Any]:
        """``POST /_synapse/admin/v1/reset_password/{user_id}`` — set a new password.

        NOTE: disabled by Synapse when delegated auth (MAS / MSC3861) is enabled.
        """
        return await self._request(
            "POST",
            f"/_synapse/admin/v1/reset_password/{user_id}",
            json={"new_password": new_password, "logout_devices": logout_devices},
        )

    async def set_shadow_ban(self, user_id: str, banned: bool) -> dict[str, Any]:
        """``POST``/``DELETE /_synapse/admin/v1/users/{user_id}/shadow_ban``."""
        method = "POST" if banned else "DELETE"
        return await self._request(method, f"/_synapse/admin/v1/users/{user_id}/shadow_ban")

    async def create_registration_token(
        self,
        *,
        uses_allowed: int | None = None,
        expiry_time: int | None = None,
        length: int | None = None,
    ) -> dict[str, Any]:
        """``POST /_synapse/admin/v1/registration_tokens/new`` — mint a token."""
        body: dict[str, Any] = {}
        if uses_allowed is not None:
            body["uses_allowed"] = uses_allowed
        if expiry_time is not None:
            body["expiry_time"] = expiry_time
        if length is not None:
            body["length"] = length
        return await self._request("POST", "/_synapse/admin/v1/registration_tokens/new", json=body)

    async def delete_registration_token(self, token: str) -> dict[str, Any]:
        """``DELETE /_synapse/admin/v1/registration_tokens/{token}``."""
        return await self._request("DELETE", f"/_synapse/admin/v1/registration_tokens/{token}")

    async def send_server_notice(
        self, user_id: str, body_text: str, *, msgtype: str = "m.text"
    ) -> dict[str, Any]:
        """``POST /_synapse/admin/v1/send_server_notice`` — message a user from the server."""
        return await self._request(
            "POST",
            "/_synapse/admin/v1/send_server_notice",
            json={"user_id": user_id, "content": {"msgtype": msgtype, "body": body_text}},
        )

    async def set_room_block(self, room_id: str, block: bool) -> dict[str, Any]:
        """``PUT /_synapse/admin/v1/rooms/{room_id}/block`` — block/unblock a room."""
        return await self._request(
            "PUT", f"/_synapse/admin/v1/rooms/{room_id}/block", json={"block": block}
        )

    async def delete_room(
        self, room_id: str, *, block: bool = False, purge: bool = True
    ) -> str:
        """``DELETE /_synapse/admin/v2/rooms/{room_id}`` — asynchronously delete a room.

        Returns a ``delete_id`` you can poll with :meth:`get_room_delete_status`.
        """
        body = await self._request(
            "DELETE", f"/_synapse/admin/v2/rooms/{room_id}", json={"block": block, "purge": purge}
        )
        delete_id: str = body.get("delete_id", "")
        return delete_id

    async def get_room_delete_status(self, delete_id: str) -> dict[str, Any]:
        """``GET /_synapse/admin/v2/rooms/delete_status/{delete_id}`` — deletion progress."""
        return await self._get(f"/_synapse/admin/v2/rooms/delete_status/{delete_id}")

    async def redact_user_events(
        self, user_id: str, *, rooms: list[str] | None = None, reason: str | None = None
    ) -> str:
        """``POST /_synapse/admin/v1/user/{user_id}/redact`` — redact a user's messages.

        ``rooms`` may be an explicit list, or an empty list to mean "all rooms the
        user is in". Returns a ``redact_id`` for :meth:`get_redact_status`.
        """
        body: dict[str, Any] = {"rooms": rooms or []}
        if reason is not None:
            body["reason"] = reason
        result = await self._request(
            "POST", f"/_synapse/admin/v1/user/{user_id}/redact", json=body
        )
        redact_id: str = result.get("redact_id", "")
        return redact_id

    async def get_redact_status(self, redact_id: str) -> dict[str, Any]:
        """``GET /_synapse/admin/v1/user/redact_status/{redact_id}`` — redaction progress."""
        return await self._get(f"/_synapse/admin/v1/user/redact_status/{redact_id}")

    async def make_room_admin(self, room_id: str, user_id: str) -> dict[str, Any]:
        """``POST /_synapse/admin/v1/rooms/{room_id}/make_room_admin``.

        Grants ``user_id`` (a local user) the highest power level available in the
        room, inviting/joining them if needed. This is how the supervision bot
        becomes a room admin without anyone else's cooperation.
        """
        return await self._request(
            "POST",
            f"/_synapse/admin/v1/rooms/{room_id}/make_room_admin",
            json={"user_id": user_id},
        )

    async def force_join(self, room_id_or_alias: str, user_id: str) -> dict[str, Any]:
        """``POST /_synapse/admin/v1/join/{room_id_or_alias}`` — force a local user to join.

        Note: the *admin* whose token is used must already be in the room with
        permission to invite. For arbitrary rooms, prefer :meth:`make_room_admin`.
        """
        return await self._request(
            "POST", f"/_synapse/admin/v1/join/{room_id_or_alias}", json={"user_id": user_id}
        )


def _bool_param(value: bool) -> str:
    """Synapse expects query booleans as the lowercase strings 'true'/'false'."""
    return "true" if value else "false"
