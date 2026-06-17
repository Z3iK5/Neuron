# SPDX-License-Identifier: Apache-2.0
"""Tests for the Supervisor orchestration logic (uses lightweight fakes)."""

from __future__ import annotations

from typing import Any

import pytest

from neuron_core import RoomListPage
from neuron_core.errors import SynapseAdminError
from neuron_supervisor.core import Supervisor, SupervisorError

BOT = "@supervisor:hs.test"


class FakeAdmin:
    def __init__(self, *, fail_room: str | None = None) -> None:
        self.calls: list[tuple[str, Any, Any]] = []
        self._fail_room = fail_room

    async def list_rooms(self, *, from_offset: int = 0, limit: int = 100, **_: Any) -> RoomListPage:
        # A single page with two rooms.
        return RoomListPage(
            rooms=[{"room_id": "!a:hs.test", "name": "A"}, {"room_id": "!b:hs.test", "name": "B"}],
            total_rooms=2,
            next_batch=None,
        )

    async def make_room_admin(self, room_id: str, user_id: str) -> dict[str, Any]:
        self.calls.append(("make_room_admin", room_id, user_id))
        if room_id == self._fail_room:
            raise SynapseAdminError(403, errcode="M_FORBIDDEN", message="nope")
        return {}

    async def redact_user_events(self, user_id: str, *, rooms: Any = None) -> str:
        self.calls.append(("redact_user_events", user_id, rooms))
        return "red-1"


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any, Any]] = []

    async def kick(
        self, room_id: str, user_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("kick", room_id, user_id, reason))
        return {}

    async def ban(
        self, room_id: str, user_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("ban", room_id, user_id, reason))
        return {}


async def test_ensure_admin_in_all_rooms_promotes_each() -> None:
    admin = FakeAdmin()
    sup = Supervisor(admin, BOT, bot=FakeBot())  # type: ignore[arg-type]
    results = await sup.ensure_admin_in_all_rooms()
    assert len(results) == 2
    assert all(r["promoted"] for r in results)
    assert ("make_room_admin", "!a:hs.test", BOT) in admin.calls
    assert ("make_room_admin", "!b:hs.test", BOT) in admin.calls


async def test_ensure_admin_records_per_room_errors() -> None:
    admin = FakeAdmin(fail_room="!b:hs.test")
    sup = Supervisor(admin, BOT)  # type: ignore[arg-type]
    results = await sup.ensure_admin_in_all_rooms()
    by_id = {r["room_id"]: r for r in results}
    assert by_id["!a:hs.test"]["promoted"] is True
    assert by_id["!b:hs.test"]["promoted"] is False
    assert by_id["!b:hs.test"]["error"] == "nope"


async def test_kick_requires_bot() -> None:
    sup = Supervisor(FakeAdmin(), BOT)  # type: ignore[arg-type]  # no bot
    with pytest.raises(SupervisorError):
        await sup.kick("!a:hs.test", "@bad:hs.test")


async def test_kick_uses_bot() -> None:
    bot = FakeBot()
    sup = Supervisor(FakeAdmin(), BOT, bot=bot)  # type: ignore[arg-type]
    await sup.kick("!a:hs.test", "@bad:hs.test", reason="spam")
    assert bot.calls == [("kick", "!a:hs.test", "@bad:hs.test", "spam")]


async def test_redact_user_uses_admin_api() -> None:
    admin = FakeAdmin()
    sup = Supervisor(admin, BOT)  # type: ignore[arg-type]
    assert await sup.redact_user("@bad:hs.test") == "red-1"
    assert ("redact_user_events", "@bad:hs.test", None) in admin.calls


async def test_promotion_without_bot_user_id_fails() -> None:
    sup = Supervisor(FakeAdmin(), "")  # type: ignore[arg-type]
    with pytest.raises(SupervisorError):
        await sup.ensure_admin("!a:hs.test")
