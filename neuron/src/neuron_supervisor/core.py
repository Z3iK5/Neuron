# SPDX-License-Identifier: Apache-2.0
"""The Supervisor: keeps a bot promoted to room-admin and moderates rooms.

The class is deliberately transport-agnostic and side-effect-light so it can be
driven two ways:

- by the **console** (an operator clicks "promote bot into all rooms", "kick", …),
- by the **background loop** in ``__main__`` (periodically re-promote the bot so
  it stays admin in newly created rooms — poll-based detection for this phase).

It uses the Admin API client for server-side actions (``make_room_admin``,
redaction) and the Client-Server API client to act *as the bot* (kick/ban).
"""

from __future__ import annotations

from typing import Any

from neuron_core import AdminClient, MatrixClient, get_logger
from neuron_core.errors import AdminApiError, NeuronError

log = get_logger(__name__)


class SupervisorError(NeuronError):
    """Raised for supervisor misconfiguration (e.g. the bot token is missing)."""


class Supervisor:
    """Coordinates room supervision and moderation."""

    def __init__(
        self,
        admin: AdminClient,
        bot_user_id: str,
        *,
        bot: MatrixClient | None = None,
    ) -> None:
        """:param admin: an Admin API client (server-admin token).
        :param bot_user_id: the bot's full Matrix ID (must be a local account).
        :param bot: a Client-Server API client authenticated as the bot. Optional:
            promotion works without it, but kick/ban require it.
        """
        self.admin = admin
        self.bot_user_id = bot_user_id
        self.bot = bot

    @property
    def bot_configured(self) -> bool:
        return self.bot is not None

    def _require_bot(self) -> MatrixClient:
        if self.bot is None:
            raise SupervisorError("The supervision bot's access token is not configured.")
        return self.bot

    def _require_bot_user(self) -> str:
        if not self.bot_user_id:
            raise SupervisorError("The supervision bot's user ID is not configured.")
        return self.bot_user_id

    # --- promotion ----------------------------------------------------------
    async def ensure_admin(self, room_id: str) -> dict[str, Any]:
        """Promote the bot to the highest power level available in one room."""
        return await self.admin.make_room_admin(room_id, self._require_bot_user())

    async def ensure_admin_in_all_rooms(
        self, *, page_size: int = 100, max_rooms: int = 1000
    ) -> list[dict[str, Any]]:
        """Promote the bot in every room on the server.

        Returns one result row per room: ``{room_id, name, promoted, error}``.
        A failure in one room is recorded and does not stop the others.
        """
        self._require_bot_user()
        results: list[dict[str, Any]] = []
        offset = 0
        while len(results) < max_rooms:
            page = await self.admin.list_rooms(from_offset=offset, limit=page_size)
            if not page.rooms:
                break
            for room in page.rooms:
                room_id = room.get("room_id", "")
                entry: dict[str, Any] = {
                    "room_id": room_id,
                    "name": room.get("name") or "",
                    "promoted": False,
                    "error": None,
                }
                try:
                    await self.admin.make_room_admin(room_id, self.bot_user_id)
                    entry["promoted"] = True
                except AdminApiError as exc:
                    entry["error"] = exc.message or exc.errcode or str(exc.status_code)
                    log.warning("could not promote in room", extra={"room_id": room_id})
                results.append(entry)
            if page.next_batch is None:
                break
            offset = page.next_batch
        return results

    # --- moderation ---------------------------------------------------------
    async def kick(
        self, room_id: str, user_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """Kick a user from a room (acts as the bot; bot must be admin there)."""
        return await self._require_bot().kick(room_id, user_id, reason=reason)

    async def ban(self, room_id: str, user_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Ban a user from a room (acts as the bot; bot must be admin there)."""
        return await self._require_bot().ban(room_id, user_id, reason=reason)

    async def redact_user(self, user_id: str, *, rooms: list[str] | None = None) -> str:
        """Redact a user's messages (server-side via the Admin API). Returns a redact_id."""
        return await self.admin.redact_user_events(user_id, rooms=rooms)
