# SPDX-License-Identifier: Apache-2.0
"""Tests for the desktop first-run setup and config logic (D1).

The GUI/tray (D2) and installers (D3) need real OSes, but the setup/config/path
logic is pure and tested here — including that a fresh data dir goes from nothing
to an admin who can actually sign in to the server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from neuron_desktop import config as config_module
from neuron_desktop import paths, setup, supervisor
from neuron_desktop.config import DesktopConfig
from neuron_server.app import create_app
from neuron_server.storage import accounts
from neuron_server.storage.database import connect_database


def test_data_dir_honours_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "custom"))
    assert paths.data_dir() == tmp_path / "custom"
    monkeypatch.delenv(paths.DATA_DIR_ENV)
    # Without the override it falls back to the platform location (non-empty).
    assert str(paths.data_dir())


def test_console_url_resolution() -> None:
    assert DesktopConfig("hs", "/d", "admin").console_url() == "http://localhost:8008"
    assert (
        DesktopConfig("hs", "/d", "admin", bind_host="0.0.0.0").console_url()
        == "http://localhost:8008"
    )
    assert (
        DesktopConfig("hs", "/d", "admin", bind_host="192.168.1.5", bind_port=9000).console_url()
        == "http://192.168.1.5:9000"
    )
    assert (
        DesktopConfig("hs", "/d", "admin", public_base_url="https://chat.example").console_url()
        == "https://chat.example"
    )


def test_to_server_settings_points_at_data_dir(tmp_path: Path) -> None:
    config = DesktopConfig("my.server", str(tmp_path), "admin")
    settings = config.to_server_settings()
    assert settings.name == "my.server"
    assert str(tmp_path / "homeserver.db") in settings.database_url
    assert settings.media_store_path == str(tmp_path / "media")
    assert settings.signing_key_path == str(tmp_path / "signing.key")
    assert settings.admin_users == "admin"


def test_config_save_load_roundtrip(tmp_path: Path) -> None:
    config = DesktopConfig("hs.test", str(tmp_path), "root", bind_port=9999)
    config_module.save(config, paths.config_path(tmp_path))
    assert config_module.load(paths.config_path(tmp_path)) == config


def test_interactive_setup_uses_defaults_and_retries_password() -> None:
    inputs = iter(["", ""])  # accept default server name and admin username
    passwords = iter(["", "x", "secret-123", "secret-123"])  # empty, mismatch, then match
    messages: list[str] = []
    config, password = setup.run_interactive_setup(
        Path("/tmp/x"),
        input_fn=lambda _prompt: next(inputs),
        getpass_fn=lambda _prompt: next(passwords),
        print_fn=messages.append,
    )
    assert config.admin_username == "admin"
    assert config.server_name == setup.default_server_name()
    assert password == "secret-123"
    assert any("did not match" in m for m in messages)


def test_ensure_admin_account_is_idempotent_and_admin(tmp_path: Path) -> None:
    settings = DesktopConfig("hs.test", str(tmp_path), "admin").to_server_settings()

    async def scenario() -> None:
        uid1 = await setup.ensure_admin_account(settings, "admin", "pw-123456")
        uid2 = await setup.ensure_admin_account(settings, "admin", "pw-123456")
        assert uid1 == uid2 == "@admin:hs.test"
        db = connect_database(settings.database_url)
        await db.connect()
        try:
            row = await accounts.get_user(db, "@admin:hs.test")
        finally:
            await db.disconnect()
        assert row is not None and row.admin

    asyncio.run(scenario())


def test_open_console_uses_injected_opener(tmp_path: Path) -> None:
    opened: list[str] = []
    config = DesktopConfig("hs", str(tmp_path), "admin", bind_port=8123)
    url = supervisor.open_console(config, opener=lambda u: bool(opened.append(u)))
    assert url == "http://localhost:8123" and opened == [url]


def test_first_run_then_admin_can_sign_in(tmp_path: Path) -> None:
    """The headline flow: empty dir → setup → admin signs in and administers."""
    config = setup.perform_first_run(
        tmp_path,
        input_fn=lambda prompt: "localhost" if "Server name" in prompt else "admin",
        getpass_fn=lambda _prompt: "first-run-pw",
        print_fn=lambda _m: None,
    )

    # Config + media dir were created.
    assert paths.config_path(tmp_path).exists()
    assert paths.media_path(tmp_path).is_dir()
    assert not setup.is_first_run(tmp_path)

    # The server, configured from this data dir, lets the admin sign in and use
    # the Synapse-compatible admin API.
    with TestClient(create_app(config.to_server_settings())) as client:
        login = client.post(
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "admin"},
                "password": "first-run-pw",
            },
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        version = client.get(
            "/_synapse/admin/v1/server_version", headers={"Authorization": f"Bearer {token}"}
        )
        assert version.status_code == 200
        assert version.json()["server_version"].startswith("Neuron")
