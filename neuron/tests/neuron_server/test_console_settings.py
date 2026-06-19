# SPDX-License-Identifier: Apache-2.0
"""Tests for the console Server-settings page (server name, doctor, registration)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_SETTINGS = "/console/settings"


def _client(tmp_path: Path, *, desktop_config: Path | None = None) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        first_user_admin=True,
        public_base_url="http://localhost:8008",
        desktop_config_path=str(desktop_config) if desktop_config else "",
    )
    return TestClient(create_app(settings))


def _desktop_config(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "config.json"
    data = {
        "server_name": "neuron.local",
        "data_dir": str(tmp_path),
        "admin_username": "admin",
        "first_user_admin": True,
        "registration_enabled": True,
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _login(client: TestClient, user: str = "founder", pw: str = "s3cret-password") -> None:
    client.post("/get-started", data={"username": user, "password": pw})
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/console/login").text)
    assert token
    client.post(
        "/console/login",
        data={"username": user, "password": pw, "csrf_token": token.group(1)},
        follow_redirects=False,
    )


def _csrf(text: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert m
    return m.group(1)


def test_settings_requires_login(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get(_SETTINGS, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/console/login"


def test_settings_shows_server_name_readonly_and_doctor(tmp_path: Path) -> None:
    cfg = _desktop_config(tmp_path)
    with _client(tmp_path, desktop_config=cfg) as client:
        _login(client)
        page = client.get(_SETTINGS)
        assert page.status_code == 200
        # Server name shown with the permanence explanation (no editable name field).
        assert "neuron.local" in page.text
        assert "permanent" in page.text
        assert 'name="server_name"' not in page.text
        # Doctor checks render with statuses.
        assert "Health check" in page.text
        assert "server name" in page.text  # a known doctor check
        # Registration is editable because a desktop config path is set.
        assert 'name="registration_enabled"' in page.text


def test_settings_save_writes_config_and_shows_restart_banner(tmp_path: Path) -> None:
    cfg = _desktop_config(tmp_path, registration_enabled=True)
    with _client(tmp_path, desktop_config=cfg) as client:
        _login(client)
        token = _csrf(client.get(_SETTINGS).text)
        # Checkbox omitted -> registration disabled.
        resp = client.post(
            _SETTINGS, data={"csrf_token": token}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert json.loads(cfg.read_text())["registration_enabled"] is False
        assert "Restart the server" in client.get(_SETTINGS).text

        # Re-enable.
        token = _csrf(client.get(_SETTINGS).text)
        client.post(
            _SETTINGS,
            data={"registration_enabled": "true", "csrf_token": token},
            follow_redirects=False,
        )
        assert json.loads(cfg.read_text())["registration_enabled"] is True


def test_settings_readonly_without_desktop_config(tmp_path: Path) -> None:
    with _client(tmp_path) as client:  # no desktop_config_path
        _login(client)
        page = client.get(_SETTINGS)
        assert page.status_code == 200
        assert 'name="registration_enabled"' not in page.text  # no editable form
        assert "Registration is" in page.text  # shown read-only instead
        # A save (CSRF token seeded from another form page) is a no-op with an explanation.
        token = _csrf(client.get("/console/users/new").text)
        resp = client.post(_SETTINGS, data={"csrf_token": token}, follow_redirects=False)
        assert resp.status_code == 303
        assert "not managed by the desktop app" in client.get(_SETTINGS).text


def test_network_checks_variant_renders(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        assert client.get(_SETTINGS + "?net=1").status_code == 200


def test_header_links_server_name_to_settings(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        # The server name next to "Sign out" links to the settings page.
        assert 'href="/console/settings"' in client.get("/console").text
