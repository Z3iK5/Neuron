# SPDX-License-Identifier: Apache-2.0
"""Tests for the admin console (read + write).

These use FastAPI's ``TestClient`` with a fake admin client that records calls,
so no real homeserver is needed. They check auth gating, CSRF protection, the
MAS-disabled guard, that write actions call the right admin methods, and that
the server-admin token never leaks to the browser.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from neuron_console.app import create_app
from neuron_console.config import ConsoleSettings
from neuron_console.deps import get_admin, get_supervisor
from neuron_core import EventReportPage, RoomListPage, UserListPage

BOT_USER_ID = "@supervisor:neuron.local"


class FakeSupervisor:
    """Records supervision actions; stands in for the real Supervisor."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []

    async def ensure_admin_in_all_rooms(self, **_: Any) -> list[dict[str, Any]]:
        self.calls.append(("ensure_admin_in_all_rooms", None, None))
        return [
            {"room_id": "!abc:neuron.local", "name": "General", "promoted": True, "error": None}
        ]

    async def ensure_admin(self, room_id: str) -> dict[str, Any]:
        self.calls.append(("ensure_admin", room_id, None))
        return {}

    async def kick(
        self, room_id: str, user_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("kick", room_id, user_id))
        return {}

    async def ban(
        self, room_id: str, user_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("ban", room_id, user_id))
        return {}

    def called(self, name: str) -> tuple[str, Any, Any] | None:
        for call in self.calls:
            if call[0] == name:
                return call
        return None

ADMIN_TOKEN = "SUPER_SECRET_ADMIN_TOKEN_should_never_leak"
CONSOLE_PASSWORD = "letmein"
SERVER_NAME = "neuron.local"


class FakeAdmin:
    """Records calls and returns canned data instead of hitting a homeserver."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def called(self, name: str) -> tuple[tuple[Any, ...], dict[str, Any]] | None:
        for n, args, kwargs in self.calls:
            if n == name:
                return args, kwargs
        return None

    # reads
    async def get_server_version(self) -> dict[str, str]:
        return {"server_version": "1.155.0", "python_version": "3.11.9"}

    async def list_users(self, **_: Any) -> UserListPage:
        return UserListPage(users=[{"name": "@alice:neuron.local", "admin": True}], total=1)

    async def get_user(self, user_id: str) -> dict[str, Any]:
        return {"name": user_id, "displayname": "Alice", "admin": True,
                "shadow_banned": False, "threepids": [], "external_ids": []}

    async def list_rooms(self, **_: Any) -> RoomListPage:
        return RoomListPage(rooms=[{"room_id": "!abc:neuron.local", "name": "General",
                                    "joined_members": 3}], total_rooms=1)

    async def get_room(self, room_id: str) -> dict[str, Any]:
        return {"name": "General", "joined_members": 3, "creator": "@alice:neuron.local"}

    async def get_room_members(self, room_id: str) -> list[str]:
        return ["@alice:neuron.local"]

    async def get_room_state(self, room_id: str) -> list[dict[str, Any]]:
        return [{"type": "m.room.create", "state_key": "", "sender": "@alice:neuron.local"}]

    async def list_event_reports(self, **_: Any) -> EventReportPage:
        return EventReportPage(event_reports=[], total=0)

    async def list_registration_tokens(self) -> list[dict[str, Any]]:
        return [{"token": "EXISTING", "uses_allowed": 5, "pending": 0, "completed": 1}]

    # writes
    async def upsert_user(self, user_id: str, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        self._record("upsert_user", user_id, **kwargs)
        return {"name": user_id}, True

    async def deactivate_user(self, user_id: str, **kwargs: Any) -> dict[str, Any]:
        self._record("deactivate_user", user_id, **kwargs)
        return {}

    async def reset_password(
        self, user_id: str, new_password: str, **kwargs: Any
    ) -> dict[str, Any]:
        self._record("reset_password", user_id, new_password, **kwargs)
        return {}

    async def set_shadow_ban(self, user_id: str, banned: bool) -> dict[str, Any]:
        self._record("set_shadow_ban", user_id, banned)
        return {}

    async def redact_user_events(self, user_id: str, **kwargs: Any) -> str:
        self._record("redact_user_events", user_id, **kwargs)
        return "red-1"

    async def get_redact_status(self, redact_id: str) -> dict[str, Any]:
        return {"status": "complete"}

    async def set_room_block(self, room_id: str, block: bool) -> dict[str, Any]:
        self._record("set_room_block", room_id, block)
        return {"block": block}

    async def delete_room(self, room_id: str, **kwargs: Any) -> str:
        self._record("delete_room", room_id, **kwargs)
        return "del-1"

    async def get_room_delete_status(self, delete_id: str) -> dict[str, Any]:
        return {"status": "complete"}

    async def create_registration_token(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_registration_token", **kwargs)
        return {"token": "NEWTOKEN"}

    async def delete_registration_token(self, token: str) -> dict[str, Any]:
        self._record("delete_registration_token", token)
        return {}

    async def send_server_notice(
        self, user_id: str, body_text: str, **kwargs: Any
    ) -> dict[str, Any]:
        self._record("send_server_notice", user_id, body_text, **kwargs)
        return {"event_id": "$evt"}


@contextmanager
def make_client(**overrides: Any) -> Iterator[tuple[TestClient, FakeAdmin]]:
    fake = FakeAdmin()
    supervisor = FakeSupervisor()
    settings = ConsoleSettings(
        _env_file=None,
        console_password=SecretStr(CONSOLE_PASSWORD),
        console_session_secret=SecretStr("unit-test-session-secret"),
        homeserver_admin_token=SecretStr(ADMIN_TOKEN),
        server_name=SERVER_NAME,
        supervisor_bot_user_id=BOT_USER_ID,
        supervisor_bot_token=SecretStr("bot-token"),
        **overrides,
    )
    app = create_app(settings)
    app.dependency_overrides[get_admin] = lambda: fake
    app.dependency_overrides[get_supervisor] = lambda: supervisor
    with TestClient(app) as client:
        client.supervisor = supervisor  # type: ignore[attr-defined]
        yield client, fake


def _login(client: TestClient) -> None:
    resp = client.post("/login", data={"password": CONSOLE_PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303


def _csrf(client: TestClient) -> str:
    """Log-in-protected page that renders the hidden CSRF field."""
    html = client.get("/users/new").text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token found in form"
    return match.group(1)


# --- auth / read --------------------------------------------------------------
def test_healthz_needs_no_auth() -> None:
    with make_client() as (client, _):
        assert client.get("/healthz").json() == {"status": "ok"}


def test_dashboard_redirects_when_not_logged_in() -> None:
    with make_client() as (client, _):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


def test_wrong_password_is_rejected() -> None:
    with make_client() as (client, _):
        resp = client.post("/login", data={"password": "nope"}, follow_redirects=False)
        assert resp.status_code == 401


def test_login_then_browse() -> None:
    with make_client() as (client, _):
        _login(client)
        assert "1.155.0" in client.get("/").text
        assert "@alice:neuron.local" in client.get("/users").text
        assert "General" in client.get("/rooms/!abc:neuron.local").text


def test_all_pages_render() -> None:
    # Load every page once to catch template errors (each renders with fake data).
    with make_client() as (client, _):
        _login(client)
        pages = [
            "/", "/users", "/users/new", "/users/@alice:neuron.local",
            "/users/@alice:neuron.local/deactivate", "/rooms", "/rooms/!abc:neuron.local",
            "/rooms/!abc:neuron.local/delete", "/redactions/red-1", "/room-deletions/del-1",
            "/registration-tokens", "/server-notice", "/reports", "/supervision",
        ]
        for path in pages:
            assert client.get(path).status_code == 200, f"{path} did not render"


def test_admin_token_never_leaks() -> None:
    with make_client() as (client, _):
        _login(client)
        for path in ("/", "/users", "/rooms", "/registration-tokens", "/server-notice"):
            assert ADMIN_TOKEN not in client.get(path).text


# --- write actions ------------------------------------------------------------
def test_create_user() -> None:
    with make_client() as (client, fake):
        _login(client)
        token = _csrf(client)
        resp = client.post(
            "/users/new",
            data={"csrf_token": token, "localpart": "bob", "password": "pw", "displayname": "Bob"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/users/@bob:neuron.local"
        call = fake.called("upsert_user")
        assert call is not None
        assert call[0][0] == "@bob:neuron.local"
        assert call[1]["password"] == "pw"


def test_deactivate_user() -> None:
    with make_client() as (client, fake):
        _login(client)
        token = _csrf(client)
        resp = client.post(
            "/users/@alice:neuron.local/deactivate",
            data={"csrf_token": token, "erase": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        call = fake.called("deactivate_user")
        assert call is not None
        assert call[0][0] == "@alice:neuron.local"
        assert call[1]["erase"] is True


def test_create_registration_token() -> None:
    with make_client() as (client, fake):
        _login(client)
        token = _csrf(client)
        resp = client.post(
            "/registration-tokens/new",
            data={"csrf_token": token, "uses_allowed": "5"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        call = fake.called("create_registration_token")
        assert call is not None and call[1]["uses_allowed"] == 5


def test_room_block_and_delete_flow() -> None:
    with make_client() as (client, fake):
        _login(client)
        token = _csrf(client)
        # Block
        resp = client.post(
            "/rooms/!abc:neuron.local/block",
            data={"csrf_token": token, "block": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert fake.called("set_room_block") == (("!abc:neuron.local", True), {})
        # Delete -> redirect to a status page that reports completion
        resp = client.post(
            "/rooms/!abc:neuron.local/delete",
            data={"csrf_token": token, "purge": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/room-deletions/del-1"
        status_page = client.get("/room-deletions/del-1")
        assert status_page.status_code == 200
        assert "complete" in status_page.text


def test_csrf_required_for_writes() -> None:
    with make_client() as (client, fake):
        _login(client)
        # Wrong/missing CSRF token -> rejected, and the admin method is NOT called.
        resp = client.post(
            "/users/@alice:neuron.local/deactivate",
            data={"csrf_token": "bogus", "erase": "false"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert fake.called("deactivate_user") is None


def test_supervision_promote_all() -> None:
    with make_client() as (client, _):
        _login(client)
        csrf = _csrf(client)
        resp = client.post(
            "/supervision/promote-all", data={"csrf_token": csrf}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert client.supervisor.called("ensure_admin_in_all_rooms") is not None  # type: ignore[attr-defined]


def test_room_promote_and_kick() -> None:
    with make_client() as (client, _):
        _login(client)
        csrf = _csrf(client)
        # Promote the bot in one room
        resp = client.post(
            "/rooms/!abc:neuron.local/promote-bot",
            data={"csrf_token": csrf}, follow_redirects=False,
        )
        assert resp.status_code == 303
        assert client.supervisor.called("ensure_admin") is not None  # type: ignore[attr-defined]
        # Kick a member
        resp = client.post(
            "/rooms/!abc:neuron.local/kick",
            data={"csrf_token": csrf, "user_id": "@bad:neuron.local"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        kick = client.supervisor.called("kick")  # type: ignore[attr-defined]
        assert kick is not None
        assert kick[1] == "!abc:neuron.local"
        assert kick[2] == "@bad:neuron.local"


def test_reset_password_blocked_under_mas() -> None:
    with make_client(auth_mode="mas") as (client, fake):
        _login(client)
        token = _csrf(client)
        resp = client.post(
            "/users/@alice:neuron.local/reset-password",
            data={"csrf_token": token, "new_password": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 409
        assert "Matrix Authentication Service" in resp.text
        assert fake.called("reset_password") is None
