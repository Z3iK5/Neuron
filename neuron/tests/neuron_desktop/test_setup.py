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


# --- existing-install detection: upgrade vs fresh --------------------------


def _make_install(base: Path, *, version: str = "0.0.1", server_name: str = "old.server") -> None:
    """Simulate a full prior install in ``base`` (config + db + key + media + welcome)."""
    base.mkdir(parents=True, exist_ok=True)
    config_module.save(
        DesktopConfig(server_name, str(base), "admin"), paths.config_path(base)
    )
    paths.media_path(base).mkdir(parents=True, exist_ok=True)
    (paths.media_path(base) / "blob").write_bytes(b"media")
    db = paths.database_path(base)
    db.write_bytes(b"sqlite")
    db.with_name(db.name + "-wal").write_bytes(b"wal")
    paths.signing_key_path(base).write_text("key", encoding="utf-8")
    setup.welcome_path(base).write_text("welcome", encoding="utf-8")
    setup.record_version(base, version)


def test_detect_existing_install_none_on_empty_dir(tmp_path: Path) -> None:
    assert setup.detect_existing_install(tmp_path) is None


def test_detect_ignores_foreign_or_corrupt_config(tmp_path: Path) -> None:
    # A directory that merely happens to contain a non-Neuron config.json (e.g. a
    # mis-pointed NEURON_DATA_DIR) must NOT be treated as ours — so the destructive
    # fresh path can never fire against unrelated files.
    paths.config_path(tmp_path).write_text("THIS IS NOT JSON", encoding="utf-8")
    (paths.media_path(tmp_path)).mkdir()
    (paths.media_path(tmp_path) / "precious").write_text("user file", encoding="utf-8")
    assert setup.detect_existing_install(tmp_path) is None
    assert setup.needs_install_choice(tmp_path, "9.9.9") is None


def test_detect_none_when_only_a_database_no_config(tmp_path: Path) -> None:
    # Without a loadable config.json we don't claim ownership (config is the marker).
    paths.database_path(tmp_path).write_bytes(b"sqlite")
    assert setup.detect_existing_install(tmp_path) is None


def test_detect_reports_postgres_backend(tmp_path: Path) -> None:
    config_module.save(
        DesktopConfig(
            "pg.server", str(tmp_path), "admin",
            database_url="postgresql://u:p@host:5432/neuron",
        ),
        paths.config_path(tmp_path),
    )
    info = setup.detect_existing_install(tmp_path)
    assert info is not None and info.uses_postgres is True


def test_detect_existing_install_reads_version_and_name(tmp_path: Path) -> None:
    _make_install(tmp_path, version="0.0.1", server_name="my.server")
    info = setup.detect_existing_install(tmp_path)
    assert info is not None
    assert info.version == "0.0.1"
    assert info.server_name == "my.server"
    assert info.has_database is True


def test_version_stamp_roundtrip(tmp_path: Path) -> None:
    assert setup.installed_version(tmp_path) is None
    setup.record_version(tmp_path, "1.2.3")
    assert setup.installed_version(tmp_path) == "1.2.3"


def test_write_config_stamps_current_version(tmp_path: Path) -> None:
    setup.write_first_run_config(
        tmp_path, setup.default_first_run_config(tmp_path), print_fn=lambda _m: None
    )
    assert setup.installed_version(tmp_path) == setup.current_app_version()


def test_needs_install_choice(tmp_path: Path) -> None:
    # Clean machine -> no choice.
    assert setup.needs_install_choice(tmp_path, "9.9.9") is None
    _make_install(tmp_path, version="0.0.1")
    # Different version -> prompt.
    assert setup.needs_install_choice(tmp_path, "9.9.9") is not None
    # Same version -> no prompt (a normal relaunch).
    assert setup.needs_install_choice(tmp_path, "0.0.1") is None


def test_purge_installation_removes_everything(tmp_path: Path) -> None:
    _make_install(tmp_path)
    setup.purge_installation(tmp_path)
    db = paths.database_path(tmp_path)
    assert not paths.config_path(tmp_path).exists()
    assert not db.exists() and not db.with_name(db.name + "-wal").exists()
    assert not paths.signing_key_path(tmp_path).exists()
    assert not paths.media_path(tmp_path).exists()
    assert not setup.welcome_path(tmp_path).exists()
    assert not paths.version_path(tmp_path).exists()


def test_prune_for_upgrade_keeps_data_drops_welcome(tmp_path: Path) -> None:
    _make_install(tmp_path)
    setup.prune_for_upgrade(tmp_path)
    assert not setup.welcome_path(tmp_path).exists()  # stale once initialized
    # Durable state is preserved across the upgrade.
    assert paths.config_path(tmp_path).exists()
    assert paths.database_path(tmp_path).exists()
    assert paths.signing_key_path(tmp_path).exists()
    assert (paths.media_path(tmp_path) / "blob").exists()


def test_prune_keeps_welcome_when_uninitialized(tmp_path: Path) -> None:
    # Config present but no database (set up, never finished) -> keep the setup
    # instructions; only a stale post-init WELCOME.txt is dropped.
    config_module.save(DesktopConfig("s", str(tmp_path), "admin"), paths.config_path(tmp_path))
    setup.welcome_path(tmp_path).write_text("welcome", encoding="utf-8")
    setup.prune_for_upgrade(tmp_path)
    assert setup.welcome_path(tmp_path).exists()


def test_purge_raises_and_keeps_config_marker_on_failure(tmp_path: Path) -> None:
    _make_install(tmp_path)
    # Force a removal failure: replace signing.key with a non-empty directory, which
    # Path.unlink() can't remove (raises OSError).
    paths.signing_key_path(tmp_path).unlink()
    keydir = paths.signing_key_path(tmp_path)
    keydir.mkdir()
    (keydir / "x").write_text("x", encoding="utf-8")

    import pytest

    with pytest.raises(OSError, match="signing.key"):
        setup.purge_installation(tmp_path)
    # The config marker survives a failed purge, so the install stays detectable and
    # the next launch re-offers the choice instead of first-running over leftovers.
    assert paths.config_path(tmp_path).exists()
    assert setup.detect_existing_install(tmp_path) is not None


def test_resolve_existing_install_upgrade(tmp_path: Path) -> None:
    _make_install(tmp_path, version="0.0.1")
    action = setup.resolve_existing_install(
        tmp_path, "9.9.9", chooser=lambda _e: setup.INSTALL_UPGRADE
    )
    assert action == setup.INSTALL_UPGRADE
    assert paths.database_path(tmp_path).exists()  # data kept
    assert not setup.welcome_path(tmp_path).exists()  # pruned


def test_resolve_existing_install_fresh(tmp_path: Path) -> None:
    _make_install(tmp_path, version="0.0.1")
    action = setup.resolve_existing_install(
        tmp_path, "9.9.9", chooser=lambda _e: setup.INSTALL_FRESH
    )
    assert action == setup.INSTALL_FRESH
    assert not paths.config_path(tmp_path).exists()  # wiped
    assert not paths.database_path(tmp_path).exists()
    assert setup.is_first_run(tmp_path)  # next launch will do first-run setup


def test_resolve_existing_install_cancel_touches_nothing(tmp_path: Path) -> None:
    _make_install(tmp_path, version="0.0.1")
    action = setup.resolve_existing_install(
        tmp_path, "9.9.9", chooser=lambda _e: setup.INSTALL_CANCEL
    )
    assert action == setup.INSTALL_CANCEL
    assert paths.config_path(tmp_path).exists()
    assert setup.welcome_path(tmp_path).exists()  # not pruned


def test_resolve_existing_install_noop_on_same_version(tmp_path: Path) -> None:
    _make_install(tmp_path, version="0.0.1")

    def _never(_e: setup.ExistingInstall) -> str:
        raise AssertionError("chooser must not be called when versions match")

    assert setup.resolve_existing_install(tmp_path, "0.0.1", chooser=_never) is None
    assert setup.welcome_path(tmp_path).exists()  # nothing removed


def test_resolve_existing_install_noop_on_clean_machine(tmp_path: Path) -> None:
    def _never(_e: setup.ExistingInstall) -> str:
        raise AssertionError("chooser must not be called on a clean machine")

    assert setup.resolve_existing_install(tmp_path, "9.9.9", chooser=_never) is None


def test_choose_via_terminal_defaults_to_upgrade(tmp_path: Path) -> None:
    info = setup.ExistingInstall(
        version="0.0.1", server_name="s", has_database=True, uses_postgres=False
    )
    # Empty answer -> upgrade.
    assert (
        setup.choose_via_terminal(info, input_fn=lambda _p: "", print_fn=lambda _m: None)
        == setup.INSTALL_UPGRADE
    )
    # 'f' without typing the confirmation word -> stays on the safe upgrade.
    answers = iter(["f", "nope"])
    assert (
        setup.choose_via_terminal(
            info, input_fn=lambda _p: next(answers), print_fn=lambda _m: None
        )
        == setup.INSTALL_UPGRADE
    )
    # 'f' then 'erase' -> fresh.
    answers2 = iter(["f", "erase"])
    assert (
        setup.choose_via_terminal(
            info, input_fn=lambda _p: next(answers2), print_fn=lambda _m: None
        )
        == setup.INSTALL_FRESH
    )
