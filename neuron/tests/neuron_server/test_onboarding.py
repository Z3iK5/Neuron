# SPDX-License-Identifier: Apache-2.0
"""Tests for the in-browser 'Get started' onboarding (account creation + connect)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_GET_STARTED = "/get-started"
_LOGIN = "/_matrix/client/v3/login"


def _client(tmp_path: Path, *, registration_enabled: bool = True) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        registration_enabled=registration_enabled,
    )
    return TestClient(create_app(settings))


def test_landing_links_to_get_started(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'href="/get-started"' in page.text


def test_get_started_renders_form_when_open(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        page = client.get(_GET_STARTED)
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "Create your account" in page.text
        assert 'name="username"' in page.text
        assert 'name="password"' in page.text
        # The connect-a-client guide always shows the homeserver address.
        assert "neuron.local" in page.text


def test_get_started_hides_form_when_closed(tmp_path: Path) -> None:
    with _client(tmp_path, registration_enabled=False) as client:
        page = client.get(_GET_STARTED)
        assert page.status_code == 200
        assert "registration is disabled" in page.text.lower()
        assert 'name="password"' not in page.text
        # Still tells the user how to connect a client.
        assert "Connect a chat app" in page.text


def test_post_creates_account_and_shows_user_id(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post(
            _GET_STARTED, data={"username": "alice", "password": "s3cret-password"}
        )
        assert resp.status_code == 200
        assert "Account created" in resp.text
        assert "@alice:neuron.local" in resp.text


def test_created_account_can_log_in(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post(
            _GET_STARTED, data={"username": "bob", "password": "s3cret-password"}
        )
        assert created.status_code == 200

        login = client.post(
            _LOGIN,
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "bob"},
                "password": "s3cret-password",
            },
        )
        assert login.status_code == 200
        body = login.json()
        assert body["user_id"] == "@bob:neuron.local"
        assert body["access_token"]


def test_duplicate_username_shows_error(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        first = client.post(
            _GET_STARTED, data={"username": "carol", "password": "s3cret-password"}
        )
        assert first.status_code == 200

        dup = client.post(
            _GET_STARTED, data={"username": "carol", "password": "another-password"}
        )
        assert dup.status_code == 400
        # The form is re-rendered with the error and the typed username preserved.
        assert "already taken" in dup.text.lower()
        assert 'value="carol"' in dup.text
        assert 'name="password"' in dup.text


def test_post_when_closed_is_forbidden(tmp_path: Path) -> None:
    with _client(tmp_path, registration_enabled=False) as client:
        resp = client.post(
            _GET_STARTED, data={"username": "dave", "password": "s3cret-password"}
        )
        assert resp.status_code == 403
        assert "registration is disabled" in resp.text.lower()
