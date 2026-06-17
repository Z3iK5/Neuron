# SPDX-License-Identifier: Apache-2.0
"""Tests for neuron_server identity & auth (HS-1), via TestClient + temp SQLite."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_WHOAMI = "/_matrix/client/v3/account/whoami"


def _client(
    tmp_path: Path, *, registration_enabled: bool = True, name: str = "neuron.local"
) -> TestClient:
    settings = NeuronServerSettings(
        name=name,
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        registration_enabled=registration_enabled,
    )
    return TestClient(create_app(settings))


def _register(
    client: TestClient,
    *,
    username: str = "alice",
    password: str = "s3cret-password",
    **extra: object,
) -> httpx.Response:
    """Run the two-step UIA (m.login.dummy) registration and return the 2nd response."""
    challenge = client.post(_REG, json={"username": username, "password": password, **extra})
    assert challenge.status_code == 401
    session = challenge.json()["session"]
    body = {
        "username": username,
        "password": password,
        "auth": {"type": "m.login.dummy", "session": session},
        **extra,
    }
    return client.post(_REG, json=body)


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_register_uia_challenge_then_success(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        challenge = client.post(_REG, json={"username": "alice", "password": "pw-123456"})
        assert challenge.status_code == 401
        body = challenge.json()
        assert body["flows"] == [{"stages": ["m.login.dummy"]}]
        assert "session" in body

        result = _register(client)
        assert result.status_code == 200
        out = result.json()
        assert out["user_id"] == "@alice:neuron.local"
        assert out["access_token"]
        assert out["device_id"]


def test_register_inhibit_login_returns_only_user_id(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        result = _register(client, username="bob", inhibit_login=True)
        assert result.status_code == 200
        out = result.json()
        assert out == {"user_id": "@bob:neuron.local"}


def test_duplicate_registration_is_user_in_use(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert _register(client).status_code == 200
        again = _register(client)
        assert again.status_code == 400
        assert again.json()["errcode"] == "M_USER_IN_USE"


def test_register_available(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        free = client.get("/_matrix/client/v3/register/available", params={"username": "bob"})
        assert free.status_code == 200 and free.json()["available"] is True

        _register(client, username="bob", password="pw-123456")
        taken = client.get("/_matrix/client/v3/register/available", params={"username": "bob"})
        assert taken.status_code == 400 and taken.json()["errcode"] == "M_USER_IN_USE"


def test_registration_disabled_is_forbidden(tmp_path: Path) -> None:
    with _client(tmp_path, registration_enabled=False) as client:
        resp = client.post(_REG, json={"username": "x", "password": "y"})
        assert resp.status_code == 403 and resp.json()["errcode"] == "M_FORBIDDEN"


def test_invalid_username_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        # Uppercase is not allowed in a localpart.
        resp = _register(client, username="NotAllowed")
        assert resp.status_code == 400
        assert resp.json()["errcode"] == "M_INVALID_USERNAME"


def test_whoami_requires_and_resolves_token(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client).json()["access_token"]

        ok = client.get(_WHOAMI, headers=_auth_header(token))
        assert ok.status_code == 200
        assert ok.json()["user_id"] == "@alice:neuron.local"
        assert ok.json()["is_guest"] is False

        missing = client.get(_WHOAMI)
        assert missing.status_code == 401 and missing.json()["errcode"] == "M_MISSING_TOKEN"

        bad = client.get(_WHOAMI, headers=_auth_header("not-a-real-token"))
        assert bad.status_code == 401 and bad.json()["errcode"] == "M_UNKNOWN_TOKEN"


def test_login_flows_and_password_login(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _register(client, username="carol", password="hunter2-aaa")

        flows = client.get("/_matrix/client/v3/login").json()
        assert {"type": "m.login.password"} in flows["flows"]

        resp = client.post(
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "carol"},
                "password": "hunter2-aaa",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "@carol:neuron.local"
        assert resp.json()["access_token"]


def test_login_wrong_password_and_unknown_user_are_forbidden(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _register(client, username="dave", password="correct-horse")

        wrong = client.post(
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "dave"},
                "password": "wrong-password",
            },
        )
        assert wrong.status_code == 403 and wrong.json()["errcode"] == "M_FORBIDDEN"

        ghost = client.post(
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "ghost"},
                "password": "whatever",
            },
        )
        assert ghost.status_code == 403


def test_logout_invalidates_token(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, username="erin", password="pw-123456").json()["access_token"]
        header = _auth_header(token)

        assert client.get(_WHOAMI, headers=header).status_code == 200
        assert client.post("/_matrix/client/v3/logout", headers=header).status_code == 200
        assert client.get(_WHOAMI, headers=header).status_code == 401


def test_device_list_get_update_delete(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        reg = _register(
            client, username="frank", password="pw-123456", initial_device_display_name="laptop"
        ).json()
        token, device_id = reg["access_token"], reg["device_id"]
        header = _auth_header(token)

        listed = client.get("/_matrix/client/v3/devices", headers=header).json()["devices"]
        assert any(d["device_id"] == device_id for d in listed)

        one = client.get(f"/_matrix/client/v3/devices/{device_id}", headers=header)
        assert one.status_code == 200 and one.json()["display_name"] == "laptop"

        renamed = client.put(
            f"/_matrix/client/v3/devices/{device_id}",
            headers=header,
            json={"display_name": "desktop"},
        )
        assert renamed.status_code == 200
        after = client.get(f"/_matrix/client/v3/devices/{device_id}", headers=header)
        assert after.json()["display_name"] == "desktop"

        # Deleting the device invalidates the token bound to it.
        deleted = client.delete(f"/_matrix/client/v3/devices/{device_id}", headers=header)
        assert deleted.status_code == 200
        assert client.get(_WHOAMI, headers=header).status_code == 401


def test_unknown_device_get_is_not_found(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client, username="grace", password="pw-123456").json()["access_token"]
        resp = client.get("/_matrix/client/v3/devices/NOPE", headers=_auth_header(token))
        assert resp.status_code == 404 and resp.json()["errcode"] == "M_NOT_FOUND"
