# SPDX-License-Identifier: Apache-2.0
"""Tests for self-serve account management: POST /v3/account/password and
POST /v3/account/deactivate, both gated behind an m.login.password UIA stage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_LOGIN = "/_matrix/client/v3/login"
_WHOAMI = "/_matrix/client/v3/account/whoami"
_PASSWORD = "/_matrix/client/v3/account/password"
_DEACTIVATE = "/_matrix/client/v3/account/deactivate"


def _client(tmp_path: Path, **overrides: Any) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        registration_enabled=True,
        **overrides,
    )
    return TestClient(create_app(settings))


def _register(
    client: TestClient, *, username: str = "alice", password: str = "s3cret-password"
) -> dict[str, Any]:
    """Run the two-step UIA (m.login.dummy) registration; return the login body."""
    challenge = client.post(_REG, json={"username": username, "password": password})
    assert challenge.status_code == 401
    session = challenge.json()["session"]
    resp = client.post(
        _REG,
        json={
            "username": username,
            "password": password,
            "auth": {"type": "m.login.dummy", "session": session},
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _login(client: TestClient, username: str, password: str) -> httpx.Response:
    return client.post(
        _LOGIN,
        json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
        },
    )


def _header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _password_auth(session: str, username: str, password: str) -> dict[str, Any]:
    return {
        "type": "m.login.password",
        "session": session,
        "identifier": {"type": "m.id.user", "user": username},
        "password": password,
    }


def _uia_challenge(client: TestClient, path: str, token: str, body: dict[str, Any]) -> str:
    """POST without auth, assert the m.login.password challenge, return the session."""
    resp = client.post(path, headers=_header(token), json=body)
    assert resp.status_code == 401
    challenge = resp.json()
    assert challenge["flows"] == [{"stages": ["m.login.password"]}]
    assert challenge["completed"] == []
    assert isinstance(challenge["session"], str)
    return challenge["session"]


# --- POST /v3/account/password ----------------------------------------------


def test_change_password_full_uia_round_trip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, username="alice", password="old-password")["access_token"]

        session = _uia_challenge(client, _PASSWORD, token, {"new_password": "new-password"})
        done = client.post(
            _PASSWORD,
            headers=_header(token),
            json={
                "new_password": "new-password",
                "auth": _password_auth(session, "alice", "old-password"),
            },
        )
        assert done.status_code == 200 and done.json() == {}

        # Old password no longer works; the new one does.
        assert _login(client, "alice", "old-password").status_code == 403
        assert _login(client, "alice", "new-password").status_code == 200


def test_change_password_accepts_auth_without_identifier(tmp_path: Path) -> None:
    # UIA re-authenticates the token's own user, so the identifier is optional.
    with _client(tmp_path) as client:
        token = _register(client, username="alice", password="old-password")["access_token"]
        session = _uia_challenge(client, _PASSWORD, token, {"new_password": "new-password"})
        done = client.post(
            _PASSWORD,
            headers=_header(token),
            json={
                "new_password": "new-password",
                "auth": {
                    "type": "m.login.password",
                    "session": session,
                    "password": "old-password",
                },
            },
        )
        assert done.status_code == 200
        assert _login(client, "alice", "new-password").status_code == 200


def test_change_password_wrong_current_password_is_forbidden(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, username="alice", password="old-password")["access_token"]
        session = _uia_challenge(client, _PASSWORD, token, {"new_password": "new-password"})
        resp = client.post(
            _PASSWORD,
            headers=_header(token),
            json={
                "new_password": "new-password",
                "auth": _password_auth(session, "alice", "not-my-password"),
            },
        )
        assert resp.status_code == 403 and resp.json()["errcode"] == "M_FORBIDDEN"
        # Nothing changed.
        assert _login(client, "alice", "old-password").status_code == 200

        # The session stays open, so a corrected retry succeeds without a new challenge.
        retry = client.post(
            _PASSWORD,
            headers=_header(token),
            json={
                "new_password": "new-password",
                "auth": _password_auth(session, "alice", "old-password"),
            },
        )
        assert retry.status_code == 200


def test_change_password_cross_user_identifier_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _register(client, username="mallory", password="mallory-pw")
        token = _register(client, username="alice", password="alice-pw")["access_token"]

        session = _uia_challenge(client, _PASSWORD, token, {"new_password": "new-password"})
        # Alice's token, but the auth identifies (and correctly authenticates) mallory.
        resp = client.post(
            _PASSWORD,
            headers=_header(token),
            json={
                "new_password": "new-password",
                "auth": _password_auth(session, "mallory", "mallory-pw"),
            },
        )
        assert resp.status_code == 403 and resp.json()["errcode"] == "M_FORBIDDEN"
        assert _login(client, "alice", "alice-pw").status_code == 200


def test_change_password_missing_new_password(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client)["access_token"]
        resp = client.post(_PASSWORD, headers=_header(token), json={})
        assert resp.status_code == 400 and resp.json()["errcode"] == "M_MISSING_PARAM"
        empty = client.post(_PASSWORD, headers=_header(token), json={"new_password": ""})
        assert empty.status_code == 400 and empty.json()["errcode"] == "M_MISSING_PARAM"


def test_change_password_logs_out_other_devices_by_default(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        first = _register(client, username="alice", password="old-password")["access_token"]
        second = _login(client, "alice", "old-password").json()["access_token"]

        session = _uia_challenge(client, _PASSWORD, first, {"new_password": "new-password"})
        done = client.post(
            _PASSWORD,
            headers=_header(first),
            json={
                "new_password": "new-password",
                "auth": _password_auth(session, "alice", "old-password"),
            },
        )
        assert done.status_code == 200

        # The requesting session survives; the other device is logged out.
        assert client.get(_WHOAMI, headers=_header(first)).status_code == 200
        assert client.get(_WHOAMI, headers=_header(second)).status_code == 401


def test_change_password_logout_devices_false_keeps_sessions(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        first = _register(client, username="alice", password="old-password")["access_token"]
        second = _login(client, "alice", "old-password").json()["access_token"]

        session = _uia_challenge(
            client, _PASSWORD, first, {"new_password": "new-password", "logout_devices": False}
        )
        done = client.post(
            _PASSWORD,
            headers=_header(first),
            json={
                "new_password": "new-password",
                "logout_devices": False,
                "auth": _password_auth(session, "alice", "old-password"),
            },
        )
        assert done.status_code == 200
        assert client.get(_WHOAMI, headers=_header(first)).status_code == 200
        assert client.get(_WHOAMI, headers=_header(second)).status_code == 200
        assert _login(client, "alice", "new-password").status_code == 200


def test_account_password_requires_access_token(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post(_PASSWORD, json={"new_password": "x"})
        assert resp.status_code == 401 and resp.json()["errcode"] == "M_MISSING_TOKEN"


# --- POST /v3/account/deactivate --------------------------------------------


def test_deactivate_full_uia_round_trip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        reg = _register(client, username="alice", password="alice-pw")
        first = reg["access_token"]
        second = _login(client, "alice", "alice-pw").json()["access_token"]

        session = _uia_challenge(client, _DEACTIVATE, first, {})
        done = client.post(
            _DEACTIVATE,
            headers=_header(first),
            json={"erase": True, "auth": _password_auth(session, "alice", "alice-pw")},
        )
        assert done.status_code == 200
        assert done.json() == {"id_server_unbind_result": "success"}

        # Every session is revoked, including the requesting one.
        assert client.get(_WHOAMI, headers=_header(first)).status_code == 401
        assert client.get(_WHOAMI, headers=_header(second)).status_code == 401

        # A deactivated account can no longer log in, even with the right password.
        again = _login(client, "alice", "alice-pw")
        assert again.status_code == 403
        assert again.json()["errcode"] == "M_USER_DEACTIVATED"


def test_deactivate_wrong_password_is_forbidden_and_harmless(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, username="alice", password="alice-pw")["access_token"]
        session = _uia_challenge(client, _DEACTIVATE, token, {})
        resp = client.post(
            _DEACTIVATE,
            headers=_header(token),
            json={"auth": _password_auth(session, "alice", "wrong-pw")},
        )
        assert resp.status_code == 403 and resp.json()["errcode"] == "M_FORBIDDEN"
        # The account is untouched.
        assert client.get(_WHOAMI, headers=_header(token)).status_code == 200
        assert _login(client, "alice", "alice-pw").status_code == 200


def test_deactivate_cross_user_identifier_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _register(client, username="mallory", password="mallory-pw")
        token = _register(client, username="alice", password="alice-pw")["access_token"]
        session = _uia_challenge(client, _DEACTIVATE, token, {})
        resp = client.post(
            _DEACTIVATE,
            headers=_header(token),
            json={"auth": _password_auth(session, "mallory", "mallory-pw")},
        )
        assert resp.status_code == 403 and resp.json()["errcode"] == "M_FORBIDDEN"
        assert _login(client, "alice", "alice-pw").status_code == 200
        assert _login(client, "mallory", "mallory-pw").status_code == 200


# --- rate limiting ------------------------------------------------------------


def test_failed_uia_password_attempts_are_rate_limited_like_login(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        rate_limit_login_burst=3,
        rate_limit_login_hz=0.001,  # effectively no refill during the test
    ) as client:
        token = _register(client, username="alice", password="alice-pw")["access_token"]
        session = _uia_challenge(client, _PASSWORD, token, {"new_password": "new-password"})

        body = {
            "new_password": "new-password",
            "auth": _password_auth(session, "alice", "wrong-pw"),
        }
        for _ in range(3):  # the burst: each failed attempt is charged
            resp = client.post(_PASSWORD, headers=_header(token), json=body)
            assert resp.status_code == 403

        limited = client.post(_PASSWORD, headers=_header(token), json=body)
        assert limited.status_code == 429
        assert limited.json()["errcode"] == "M_LIMIT_EXCEEDED"
        assert isinstance(limited.json()["retry_after_ms"], int)


def test_uia_and_login_share_the_per_account_budget(tmp_path: Path) -> None:
    # The per-account bucket is keyed on the full Matrix ID for both /login and
    # the UIA stage, so an attacker can't alternate endpoints to double the budget.
    with _client(
        tmp_path, rate_limit_login_burst=3, rate_limit_login_hz=0.001
    ) as client:
        token = _register(client, username="alice", password="alice-pw")["access_token"]
        session = _uia_challenge(client, _PASSWORD, token, {"new_password": "new-password"})

        assert _login(client, "alice", "wrong-pw").status_code == 403  # 1
        assert _login(client, "alice", "wrong-pw").status_code == 403  # 2
        body = {
            "new_password": "new-password",
            "auth": _password_auth(session, "alice", "wrong-pw"),
        }
        assert client.post(_PASSWORD, headers=_header(token), json=body).status_code == 403  # 3
        limited = client.post(_PASSWORD, headers=_header(token), json=body)
        assert limited.status_code == 429
        assert limited.json()["errcode"] == "M_LIMIT_EXCEEDED"
