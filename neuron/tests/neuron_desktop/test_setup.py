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
from neuron_desktop import paths, process, setup, supervisor
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
    config = DesktopConfig(
        "hs.test", str(tmp_path), "root", bind_port=9999,
        database_url="postgresql://u:p@db/neuron", db_pool_size=8,
    )
    config_module.save(config, paths.config_path(tmp_path))
    assert config_module.load(paths.config_path(tmp_path)) == config


def test_config_loads_without_database_fields(tmp_path: Path) -> None:
    """Backward compat: a config.json written before the DB fields existed loads,
    defaulting to the built-in SQLite backend."""
    import json

    cfg_file = paths.config_path(tmp_path)
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(
        json.dumps({"server_name": "old", "data_dir": str(tmp_path), "admin_username": "admin"}),
        encoding="utf-8",
    )
    cfg = config_module.load(cfg_file)
    assert cfg.database_url == "" and cfg.db_pool_size == 1
    assert cfg.uses_postgres is False


def test_to_server_settings_postgres_backend(tmp_path: Path) -> None:
    config = DesktopConfig(
        "pg.server", str(tmp_path), "admin",
        database_url="postgresql://u:p@db:5432/neuron", db_pool_size=8,
    )
    settings = config.to_server_settings()
    assert settings.database_url == "postgresql://u:p@db:5432/neuron"
    assert settings.db_pool_size == 8
    assert config.uses_postgres is True


def test_validate_database_url() -> None:
    assert config_module.validate_database_url("") is None  # blank => SQLite
    assert config_module.validate_database_url("   ") is None
    assert config_module.validate_database_url("postgresql://u:p@h:5432/db") is None
    assert config_module.validate_database_url("postgres://u@h/db") is None
    assert config_module.validate_database_url("mysql://x") is not None
    assert config_module.validate_database_url("/var/lib/pg") is not None


def test_config_to_env_includes_database_and_pool_size(tmp_path: Path) -> None:
    config = DesktopConfig(
        "s", str(tmp_path), "admin",
        database_url="postgresql://u:p@db/neuron", db_pool_size=8,
    )
    env = process.config_to_env(config)
    assert env["NEURON_SERVER_DATABASE_URL"] == "postgresql://u:p@db/neuron"
    assert env["NEURON_SERVER_DB_POOL_SIZE"] == "8"


def test_interactive_setup_uses_defaults_and_retries_password() -> None:
    # Default server name + admin username, then blank database (built-in SQLite).
    inputs = iter(["", "", ""])
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
    assert config.database_url == ""  # blank => built-in SQLite
    assert config.db_pool_size == 1
    assert password == "secret-123"
    assert any("did not match" in m for m in messages)


def test_interactive_setup_postgres_backend() -> None:
    answers = [
        ("Server name", "pg.server"),
        ("Admin username", "root"),
        ("PostgreSQL URL", "postgresql://u:p@db:5432/neuron"),
        ("pool size", "8"),
    ]

    def _input(prompt: str) -> str:
        for key, val in answers:
            if key in prompt:
                return val
        return ""

    config, password = setup.run_interactive_setup(
        Path("/tmp/x"),
        input_fn=_input,
        getpass_fn=lambda _prompt: "secret-123",
        print_fn=lambda _m: None,
    )
    assert config.database_url == "postgresql://u:p@db:5432/neuron"
    assert config.db_pool_size == 8
    assert config.to_server_settings().database_url == "postgresql://u:p@db:5432/neuron"


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
    # The desktop "open console" action opens the built-in admin console at /console.
    assert url == "http://localhost:8123/console" and opened == [url]


def _register_via_browser(client: TestClient, username: str, password: str) -> None:
    """Create an account through the in-browser /get-started onboarding form."""
    resp = client.post("/get-started", data={"username": username, "password": password})
    assert resp.status_code == 200, resp.text


def _is_admin(client: TestClient, username: str, password: str) -> bool:
    login = client.post(
        "/_matrix/client/v3/login",
        json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
        },
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    # The Synapse-compatible admin API is admin-gated.
    return (
        client.get(
            "/_synapse/admin/v1/server_version", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )


def test_noninteractive_first_run_sets_up_browser_signup(tmp_path: Path) -> None:
    """A double-clicked app (no stdin) sets up without prompts and without a default
    password — the first account created in the browser becomes the admin."""
    config = setup.perform_noninteractive_first_run(tmp_path, print_fn=lambda _m: None)

    assert config.first_user_admin is True
    assert paths.config_path(tmp_path).exists()
    assert paths.media_path(tmp_path).is_dir()
    assert not setup.is_first_run(tmp_path)

    # The welcome file points at the in-browser sign-up (no password in it).
    welcome_text = setup.welcome_path(tmp_path).read_text(encoding="utf-8")
    assert "/get-started" in welcome_text
    assert "Password" not in welcome_text

    # No account exists yet; the first one to sign up in the browser is the admin,
    # the next one is not.
    with TestClient(create_app(config.to_server_settings())) as client:
        _register_via_browser(client, "alice", "s3cret-password")
        _register_via_browser(client, "bob", "s3cret-password")
        assert _is_admin(client, "alice", "s3cret-password") is True
        assert _is_admin(client, "bob", "s3cret-password") is False


def test_load_or_create_is_noninteractive_without_a_tty(tmp_path: Path) -> None:
    """Under pytest stdin is not a tty, so load_or_create must not call input()."""
    assert not setup.stdin_is_interactive()
    # Would raise "lost sys.stdin" if it tried to prompt; instead it auto-sets-up.
    config = setup.load_or_create(tmp_path, print_fn=lambda _m: None)
    assert config.first_user_admin is True
    assert setup.welcome_path(tmp_path).exists()
    assert not setup.is_first_run(tmp_path)


def test_first_run_then_admin_can_sign_in(tmp_path: Path) -> None:
    """The headline flow: empty dir → setup → admin signs in and administers."""
    def _input(prompt: str) -> str:
        if "Server name" in prompt:
            return "localhost"
        if "PostgreSQL" in prompt:  # blank => built-in SQLite
            return ""
        return "admin"

    config = setup.perform_first_run(
        tmp_path,
        input_fn=_input,
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
