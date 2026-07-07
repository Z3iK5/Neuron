# SPDX-License-Identifier: Apache-2.0
"""Refresh tokens (CS API v1.3 / Element X): opt-in refresh, rotation, and the
expired-access-token soft-logout — all in the default non-OIDC mode."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_LOGIN = "/_matrix/client/v3/login"
_REFRESH = "/_matrix/client/v3/refresh"
_WHOAMI = "/_matrix/client/v3/account/whoami"


def _client(tmp_path: Path, **overrides: Any) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        registration_enabled=True,
        **overrides,
    )
    return TestClient(create_app(settings))


def _register(client: TestClient, *, username: str = "alice", **extra: Any) -> dict[str, Any]:
    challenge = client.post(_REG, json={"username": username, "password": "pw-123456"})
    assert challenge.status_code == 401
    session = challenge.json()["session"]
    resp = client.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
            **extra,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_register_with_refresh_returns_refresh_and_expiry(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        out = _register(client, refresh_token=True)
        assert out["access_token"]
        assert out["refresh_token"]
        assert out["expires_in_ms"] == 3_600_000
        # The freshly-issued access token authenticates.
        assert client.get(_WHOAMI, headers=_auth(out["access_token"])).status_code == 200


def test_login_without_refresh_has_no_refresh_fields(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _register(client, username="bob")
        resp = client.post(
            _LOGIN,
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "bob"},
                "password": "pw-123456",
            },
        )
        body = resp.json()
        assert "refresh_token" not in body
        assert "expires_in_ms" not in body


def test_refresh_rotates_and_invalidates_old_token(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        out = _register(client, refresh_token=True)
        old_refresh = out["refresh_token"]

        refreshed = client.post(_REFRESH, json={"refresh_token": old_refresh})
        assert refreshed.status_code == 200
        body = refreshed.json()
        assert body["access_token"] and body["access_token"] != out["access_token"]
        assert body["refresh_token"] and body["refresh_token"] != old_refresh
        assert body["expires_in_ms"] == 3_600_000

        # The new access token works.
        assert client.get(_WHOAMI, headers=_auth(body["access_token"])).status_code == 200

        # Reusing the consumed refresh token is rejected (single-use rotation).
        replay = client.post(_REFRESH, json={"refresh_token": old_refresh})
        assert replay.status_code == 401 and replay.json()["errcode"] == "M_UNKNOWN_TOKEN"


def test_unknown_refresh_token_is_unauthorized(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post(_REFRESH, json={"refresh_token": "not-a-real-token"})
        assert resp.status_code == 401 and resp.json()["errcode"] == "M_UNKNOWN_TOKEN"


def test_expired_access_token_soft_logout(tmp_path: Path) -> None:
    # A 1ms lifetime so the refreshable access token lapses immediately.
    with _client(tmp_path, access_token_lifetime_ms=1) as client:
        out = _register(client, refresh_token=True)
        time.sleep(0.05)
        resp = client.get(_WHOAMI, headers=_auth(out["access_token"]))
        assert resp.status_code == 401
        body = resp.json()
        assert body["errcode"] == "M_UNKNOWN_TOKEN"
        assert body["soft_logout"] is True

        # The client silently refreshes and is authenticated again.
        refreshed = client.post(_REFRESH, json={"refresh_token": out["refresh_token"]})
        assert refreshed.status_code == 200


def test_classic_token_never_expires_even_with_short_lifetime(tmp_path: Path) -> None:
    # Non-refresh login issues a token with no expiry; the lifetime is irrelevant.
    with _client(tmp_path, access_token_lifetime_ms=1) as client:
        out = _register(client, username="carol")  # no refresh_token
        assert "expires_in_ms" not in out
        time.sleep(0.05)
        assert client.get(_WHOAMI, headers=_auth(out["access_token"])).status_code == 200


def test_truly_unknown_token_has_no_soft_logout(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get(_WHOAMI, headers=_auth("nope"))
        assert resp.status_code == 401
        assert resp.json()["errcode"] == "M_UNKNOWN_TOKEN"
        assert "soft_logout" not in resp.json()
