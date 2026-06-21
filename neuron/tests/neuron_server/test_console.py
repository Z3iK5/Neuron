# SPDX-License-Identifier: Apache-2.0
"""Tests for the built-in admin console merged into ``neuron_server``.

The console is served by the same app as the Matrix API, authenticates the
operator's own admin account via a session cookie, and drives the in-process
``AdminService``. These tests use ``TestClient`` (which persists cookies across
requests, so the session + CSRF flow works) against a temporary SQLite database.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_LOGIN = "/console/login"


def _client(tmp_path: Path) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        first_user_admin=True,  # first account created becomes the server admin
        public_base_url="http://localhost:8008",
    )
    return TestClient(create_app(settings))


def _csrf(text: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert m, "no CSRF token found in page"
    return m.group(1)


def _signup(client: TestClient, username: str, password: str) -> None:
    """Create an account through the public onboarding form."""
    resp = client.post("/get-started", data={"username": username, "password": password})
    assert resp.status_code == 200, resp.text


def _console_login(client: TestClient, username: str, password: str):
    page = client.get(_LOGIN)
    token = _csrf(page.text)
    return client.post(
        _LOGIN,
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=False,
    )


# --- auth gating ------------------------------------------------------------
def test_anonymous_is_redirected_to_login(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for path in ("/console", "/console/users", "/console/rooms", "/console/invites"):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == _LOGIN


def test_login_page_renders_branded_card(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")  # an account must exist to sign in
        page = client.get(_LOGIN)
        assert page.status_code == 200
        assert "Sign in to your homeserver" in page.text
        assert 'name="username"' in page.text and 'name="password"' in page.text


def test_login_redirects_to_get_started_on_empty_server(tmp_path: Path) -> None:
    # A fresh server has no account, so "Open console" / the login page should send
    # the operator to create the first account rather than a dead-end login.
    with _client(tmp_path) as client:
        resp = client.get(_LOGIN, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/get-started"


def test_wrong_password_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        resp = _console_login(client, "admin", "wrong-password")
        assert resp.status_code == 401
        assert "Incorrect username or password" in resp.text


def test_non_admin_account_is_forbidden(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")  # first user -> admin
        _signup(client, "bob", "s3cret-password")  # second user -> not admin
        resp = _console_login(client, "bob", "s3cret-password")
        assert resp.status_code == 403
        assert "not a server administrator" in resp.text


def test_admin_login_opens_overview(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        resp = _console_login(client, "admin", "s3cret-password")
        assert resp.status_code == 303 and resp.headers["location"] == "/console"
        overview = client.get("/console")
        assert overview.status_code == 200
        assert "Overview" in overview.text
        # Stat cards show real counts from the in-process service.
        assert "Users" in overview.text and "Rooms" in overview.text
        # The Server card reflects the real running version (not a hard-coded 0.0.1).
        from importlib.metadata import version

        assert version("neuron") in overview.text


# --- user management --------------------------------------------------------
def test_admin_can_create_user_via_console(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _console_login(client, "admin", "s3cret-password")

        token = _csrf(client.get("/console/users/new").text)
        resp = client.post(
            "/console/users/new",
            data={
                "localpart": "carol",
                "password": "carol-password",
                "displayname": "Carol",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        listing = client.get("/console/users")
        assert "@carol:neuron.local" in listing.text
        # The created account can sign in to Matrix.
        login = client.post(
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "carol"},
                "password": "carol-password",
            },
        )
        assert login.status_code == 200


def test_grant_admin_lets_the_user_into_the_console(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _signup(client, "dave", "dave-password")  # non-admin initially
        _console_login(client, "admin", "s3cret-password")

        uid = "@dave:neuron.local"
        token = _csrf(client.get(f"/console/users/{uid}").text)
        resp = client.post(
            f"/console/users/{uid}/admin",
            data={"admin": "true", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Dave is now an admin: a fresh client can sign him into the console.
        with _client_reuse(client) as dave:
            assert _console_login(dave, "dave", "dave-password").status_code == 303


def test_reset_password_and_deactivate(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _signup(client, "erin", "old-password")
        _console_login(client, "admin", "s3cret-password")

        uid = "@erin:neuron.local"
        token = _csrf(client.get(f"/console/users/{uid}").text)
        assert client.post(
            f"/console/users/{uid}/reset-password",
            data={"new_password": "new-password", "csrf_token": token},
            follow_redirects=False,
        ).status_code == 303

        token = _csrf(client.get(f"/console/users/{uid}").text)
        assert client.post(
            f"/console/users/{uid}/deactivate",
            data={"csrf_token": token},
            follow_redirects=False,
        ).status_code == 303
        detail = client.get(f"/console/users/{uid}")
        assert "deactivated" in detail.text


def test_settings_is_in_the_nav(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _console_login(client, "admin", "s3cret-password")
        assert 'href="/console/settings"' in client.get("/console").text


def test_reactivate_user(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _signup(client, "frank", "pw-123456")
        _console_login(client, "admin", "s3cret-password")
        uid = "@frank:neuron.local"

        token = _csrf(client.get(f"/console/users/{uid}").text)
        client.post(f"/console/users/{uid}/deactivate", data={"csrf_token": token})
        detail = client.get(f"/console/users/{uid}").text
        assert "Reactivate account" in detail  # the control appears when deactivated

        token = _csrf(client.get(f"/console/users/{uid}").text)
        assert client.post(
            f"/console/users/{uid}/reactivate",
            data={"csrf_token": token},
            follow_redirects=False,
        ).status_code == 303
        # Back to active: the Deactivate control returns, Reactivate is gone.
        detail = client.get(f"/console/users/{uid}").text
        assert "Deactivate account" in detail and "Reactivate account" not in detail
        # And the reactivated account can authenticate over the Matrix API again.
        login = client.post(
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "frank"},
                "password": "pw-123456",
            },
        )
        assert login.status_code == 200


def test_edit_displayname(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _signup(client, "grace", "pw-123456")
        _console_login(client, "admin", "s3cret-password")
        uid = "@grace:neuron.local"
        token = _csrf(client.get(f"/console/users/{uid}").text)
        client.post(
            f"/console/users/{uid}/profile",
            data={"displayname": "Grace H", "csrf_token": token},
        )
        assert "Grace H" in client.get(f"/console/users/{uid}").text


def test_users_status_filter(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _signup(client, "heidi", "pw-123456")
        _console_login(client, "admin", "s3cret-password")
        uid = "@heidi:neuron.local"
        token = _csrf(client.get(f"/console/users/{uid}").text)
        client.post(f"/console/users/{uid}/deactivate", data={"csrf_token": token})

        assert "heidi" in client.get("/console/users?status=deactivated").text
        assert "heidi" not in client.get("/console/users?status=active").text


def test_user_devices_panel_and_force_logout(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _signup(client, "ivan", "pw-123456")
        # ivan signs in over the Matrix API, creating a device + access token.
        login = client.post(
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "ivan"},
                "password": "pw-123456",
            },
        )
        assert login.status_code == 200
        device_id, token = login.json()["device_id"], login.json()["access_token"]
        _console_login(client, "admin", "s3cret-password")
        uid = "@ivan:neuron.local"

        detail = client.get(f"/console/users/{uid}").text
        assert "Devices &amp; sessions" in detail and device_id in detail
        assert "Not in any rooms." in detail  # the Rooms panel renders (empty)

        # Deleting the device revokes its access token.
        csrf = _csrf(detail)
        assert client.post(
            f"/console/users/{uid}/devices/{device_id}/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        ).status_code == 303
        who = client.get(
            "/_matrix/client/v3/account/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert who.status_code == 401  # token no longer valid

        # Force-logout-all is reachable and succeeds even with no devices left.
        csrf = _csrf(client.get(f"/console/users/{uid}").text)
        assert client.post(
            f"/console/users/{uid}/logout", data={"csrf_token": csrf}, follow_redirects=False
        ).status_code == 303


# --- invites ----------------------------------------------------------------
def test_invites_create_qr_and_delete(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _console_login(client, "admin", "s3cret-password")

        token = _csrf(client.get("/console/invites").text)
        assert client.post(
            "/console/invites/new",
            data={"uses_allowed": "5", "csrf_token": token},
            follow_redirects=False,
        ).status_code == 303

        page = client.get("/console/invites")
        m = re.search(r"/console/invites/([^/]+)/qr\.svg", page.text)
        assert m, "no invite QR link rendered"
        qr = client.get(f"/console/invites/{m.group(1)}/qr.svg")
        assert qr.status_code == 200 and qr.headers["content-type"] == "image/svg+xml"
        assert "<svg" in qr.text

        invite_token = m.group(1)
        token = _csrf(client.get("/console/invites").text)
        assert client.post(
            f"/console/invites/{invite_token}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        ).status_code == 303


# --- security ---------------------------------------------------------------
def test_bad_csrf_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _console_login(client, "admin", "s3cret-password")
        resp = client.post(
            "/console/users/new",
            data={"localpart": "x", "password": "y", "csrf_token": "not-the-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 400


def test_logout_clears_the_session(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _console_login(client, "admin", "s3cret-password")
        assert client.get("/console").status_code == 200
        assert client.get("/console/logout", follow_redirects=False).status_code == 303
        assert client.get("/console", follow_redirects=False).status_code == 303


def test_bulk_deactivate_users(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _signup(client, "bob", "bob-password-12")
        _signup(client, "carol", "carol-password-12")
        _console_login(client, "admin", "s3cret-password")

        token = _csrf(client.get("/console/users").text)
        resp = client.post(
            "/console/users/bulk",
            data={
                "action": "deactivate",
                "csrf_token": token,
                "user_ids": ["@bob:neuron.local", "@carol:neuron.local"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        page = client.get("/console/users").text
        assert "Deactivated 2 user(s)." in page
        # admin stays active; only bob + carol show the deactivated pill.
        assert page.count(">deactivated</span>") == 2


def test_bulk_action_requires_csrf(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _signup(client, "bob", "bob-password-12")
        _console_login(client, "admin", "s3cret-password")
        resp = client.post(
            "/console/users/bulk",
            data={"action": "deactivate", "user_ids": ["@bob:neuron.local"]},
            follow_redirects=False,
        )
        # Missing CSRF token -> the request is rejected, not processed.
        assert resp.status_code != 303
        assert ">deactivated</span>" not in client.get("/console/users").text


def _client_reuse(existing: TestClient) -> TestClient:
    """A second TestClient over the SAME app (fresh cookies) — for multi-user tests."""
    return TestClient(existing.app)
