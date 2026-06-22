# SPDX-License-Identifier: Apache-2.0
"""First-run setup: pick a server name, choose an admin, create the account.

The interactive prompts take injected ``input``/``getpass``/``print`` callables so
the flow is unit-testable without a real terminal. Account creation talks to the
same storage the server uses, so the admin can immediately sign in to the console.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getpass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

from neuron_desktop import config as config_module
from neuron_desktop import paths
from neuron_desktop.config import DesktopConfig
from neuron_server.auth.passwords import hash_password
from neuron_server.config import NeuronServerSettings
from neuron_server.storage import accounts
from neuron_server.storage.database import connect_database
from neuron_server.storage.migrations import run_migrations

InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]


def is_first_run(base: Path) -> bool:
    """True when no desktop config exists yet in ``base``."""
    return not paths.config_path(base).exists()


# --- existing-install detection: upgrade vs fresh install -------------------

INSTALL_UPGRADE = "upgrade"
INSTALL_FRESH = "fresh"
INSTALL_CANCEL = "cancel"


@dataclass(frozen=True)
class ExistingInstall:
    """What was found in the data dir from a previous install/version."""

    version: str | None  # app version that last ran here (None if pre-versioning)
    server_name: str | None  # from config.json
    has_database: bool  # a local SQLite database file is present
    uses_postgres: bool  # the config points at an external PostgreSQL backend


def current_app_version() -> str:
    """The running app's version (from package metadata; '0.0.0' if unavailable)."""
    try:
        return _pkg_version("neuron")
    except PackageNotFoundError:  # pragma: no cover - metadata present when installed
        return "0.0.0"


def installed_version(base: Path) -> str | None:
    """The version recorded in the data dir, or None if there's no stamp."""
    path = paths.version_path(base)
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def record_version(base: Path, version: str) -> None:
    """Stamp ``version`` as the one that has configured/run this data dir."""
    base.mkdir(parents=True, exist_ok=True)
    paths.version_path(base).write_text(version, encoding="utf-8")


def detect_existing_install(base: Path) -> ExistingInstall | None:
    """Describe a prior Neuron installation in ``base``, or None if there's none.

    Ownership is proven by a **loadable** desktop ``config.json`` — the file Neuron
    writes for every install. A missing, unreadable, or foreign config means we do
    NOT treat the directory as ours, so the destructive fresh-install path can never
    fire against an unrelated folder (important when ``NEURON_DATA_DIR`` points at a
    shared/home directory).
    """
    config_file = paths.config_path(base)
    if not config_file.exists():
        return None
    try:
        config = config_module.load(config_file)
    except Exception:  # noqa: BLE001 - not a readable Neuron config -> not our dir
        return None
    return ExistingInstall(
        version=installed_version(base),
        server_name=config.server_name,
        has_database=paths.database_path(base).exists(),
        uses_postgres=config.uses_postgres,
    )


def needs_install_choice(base: Path, current_version: str) -> ExistingInstall | None:
    """Return the existing install when the user should pick upgrade-vs-fresh.

    Only when there IS a prior install of a *different* (or unknown) version — a
    same-version relaunch and a clean machine both return None (no prompt).
    """
    existing = detect_existing_install(base)
    if existing is None or existing.version == current_version:
        return None
    return existing


def purge_installation(base: Path) -> None:
    """Fresh install: remove everything pertaining to the previous install.

    Deletes the SQLite database (and its -wal/-shm sidecars), the signing key, the
    media blobs, the welcome file, the version stamp, and finally the desktop config.

    Order and atomicity matter: ``config.json`` is the ownership marker
    :func:`detect_existing_install` keys on, so it is removed **last** and only once
    everything else is gone. If any removal fails (e.g. a file is locked by a still-
    running server on Windows), we raise with the leftovers named instead of leaving
    a half-erased directory — the config survives, so the next launch still detects
    the install and re-offers the choice rather than silently first-running over the
    old database / federation key.

    A PostgreSQL-backed install keeps its data in the external database, which this
    cannot (and must not) drop; only local state is removed (see the warning shown
    before this runs).
    """
    db = paths.database_path(base)
    failed: list[str] = []

    media = paths.media_path(base)
    shutil.rmtree(media, ignore_errors=True)
    if media.exists():
        failed.append(str(media))

    for path in (
        db,
        db.with_name(db.name + "-wal"),
        db.with_name(db.name + "-shm"),
        paths.signing_key_path(base),
        welcome_path(base),
        paths.version_path(base),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed.append(str(path))

    # Remove the ownership marker only after the rest is gone.
    if not failed:
        try:
            paths.config_path(base).unlink(missing_ok=True)
        except OSError:
            failed.append(str(paths.config_path(base)))

    if failed:
        raise OSError(
            "Could not fully remove the existing installation: "
            + ", ".join(failed)
            + ". Stop any running Neuron server and try again."
        )


def prune_for_upgrade(base: Path) -> None:
    """Upgrade: remove only what an upgrade doesn't need, keeping all durable data.

    The database, signing key, config and media are preserved (the server migrates
    the schema on start). The only thing dropped is a now-stale WELCOME.txt — and
    only once the server has been initialized (its database exists), so an install
    that was set up but never finished in the browser keeps its setup instructions.
    """
    if paths.database_path(base).exists():
        welcome_path(base).unlink(missing_ok=True)


Chooser = Callable[["ExistingInstall"], str]


def choose_via_terminal(
    existing: ExistingInstall, *, input_fn: InputFn = input, print_fn: PrintFn = print
) -> str:
    """Ask on the terminal whether to upgrade or do a fresh install."""
    found = "Found an existing Neuron installation"
    if existing.version:
        found += f" (version {existing.version})"
    if existing.server_name:
        found += f" for server '{existing.server_name}'"
    print_fn(found + ".")
    print_fn("  [U] Upgrade — keep your data (database, accounts, media, signing key)")
    print_fn("  [F] Fresh install — ERASE all existing data and start over")
    if existing.uses_postgres:
        print_fn(
            "  NOTE: this server uses an external PostgreSQL database — a fresh install"
            " removes local files only; drop/recreate the Postgres database yourself."
        )
    answer = input_fn("Upgrade or fresh install? [U/f]: ").strip().lower()
    if answer in ("f", "fresh"):
        confirm = input_fn(
            "This permanently deletes the existing server's data. Type 'erase' to confirm: "
        ).strip().lower()
        return INSTALL_FRESH if confirm == "erase" else INSTALL_UPGRADE
    return INSTALL_UPGRADE


def default_install_chooser(existing: ExistingInstall) -> str:
    """Prompt on a terminal if one is attached; otherwise upgrade (never auto-erase)."""
    if stdin_is_interactive():
        return choose_via_terminal(existing)
    return INSTALL_UPGRADE


def resolve_existing_install(
    base: Path, current_version: str, *, chooser: Chooser = default_install_chooser
) -> str | None:
    """Detect a prior install of another version and act on the chosen action.

    Returns the action taken (``INSTALL_UPGRADE``/``INSTALL_FRESH``), ``INSTALL_CANCEL``
    if the user backed out, or ``None`` when there was nothing to decide (clean
    machine or same-version relaunch). On fresh it purges; on upgrade it prunes.
    """
    existing = needs_install_choice(base, current_version)
    if existing is None:
        return None
    choice = chooser(existing)
    if choice == INSTALL_FRESH:
        purge_installation(base)
        return INSTALL_FRESH
    if choice == INSTALL_CANCEL:
        return INSTALL_CANCEL
    prune_for_upgrade(base)
    return INSTALL_UPGRADE


def stdin_is_interactive() -> bool:
    """True only when we can actually prompt on a terminal.

    A double-clicked / frozen GUI app has no console, so ``sys.stdin`` is ``None``
    (or not a tty) and calling ``input()`` raises ``RuntimeError: lost sys.stdin``.
    We use this to choose interactive vs. non-interactive first-run setup.
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def welcome_path(base: Path) -> Path:
    """The file where a non-interactive first run records the admin credentials."""
    return base / "WELCOME.txt"


def default_server_name() -> str:
    """A sensible default server name derived from the machine's hostname."""
    host = (socket.gethostname() or "").split(".")[0].strip().lower()
    return host or "localhost"


def run_interactive_setup(
    base: Path,
    *,
    input_fn: InputFn = input,
    getpass_fn: InputFn = getpass,
    print_fn: PrintFn = print,
) -> tuple[DesktopConfig, str]:
    """Prompt for the first-run choices; return the config and admin password."""
    print_fn("Welcome to Neuron — let's set up your homeserver.")
    default_name = default_server_name()
    server_name = input_fn(f"Server name [{default_name}]: ").strip() or default_name
    admin_username = input_fn("Admin username [admin]: ").strip() or "admin"

    print_fn(
        "Database — leave blank for the built-in SQLite (personal / small servers);"
        " for a medium/large deployment enter a PostgreSQL URL."
    )
    while True:
        database_url = input_fn("PostgreSQL URL [blank = SQLite]: ").strip()
        err = config_module.validate_database_url(database_url)
        if err is None:
            break
        print_fn(err)
    db_pool_size = 1
    if database_url:
        raw = input_fn("PostgreSQL connection pool size [1]: ").strip()
        try:
            db_pool_size = max(1, int(raw)) if raw else 1
        except ValueError:
            db_pool_size = 1

    while True:
        password = getpass_fn("Admin password: ")
        confirm = getpass_fn("Confirm password: ")
        if password and password == confirm:
            break
        print_fn("Passwords were empty or did not match — please try again.")

    config = DesktopConfig(
        server_name=server_name,
        data_dir=str(base),
        admin_username=admin_username,
        database_url=database_url,
        db_pool_size=db_pool_size,
    )
    return config, password


async def ensure_admin_account(
    settings: NeuronServerSettings, username: str, password: str
) -> str:
    """Create the admin account (idempotently) and return its full user ID."""
    user_id = f"@{username}:{settings.name}"
    db = connect_database(settings.database_url)
    await db.connect()
    try:
        await run_migrations(db)
        if await accounts.get_user(db, user_id) is None:
            await accounts.create_user(
                db, user_id, hash_password(password), True, int(time.time() * 1000)
            )
    finally:
        await db.disconnect()
    return user_id


def _write_config(base: Path, config: DesktopConfig) -> None:
    """Create the data directories and persist the desktop config."""
    base.mkdir(parents=True, exist_ok=True)
    paths.media_path(base).mkdir(parents=True, exist_ok=True)
    config_module.save(config, paths.config_path(base))
    # Stamp the version that wrote this data dir so a later launch can tell an
    # upgrade (different version) from a normal relaunch (same version).
    record_version(base, current_app_version())


def _finalize_first_run(
    base: Path, config: DesktopConfig, password: str, *, print_fn: PrintFn
) -> None:
    """Write the config and create the admin account (interactive setup)."""
    _write_config(base, config)
    settings = config.to_server_settings()
    asyncio.run(ensure_admin_account(settings, config.admin_username, password))
    print_fn(f"Created admin @{config.admin_username}:{config.server_name}.")
    print_fn(f"State directory: {base}")


def _write_welcome_file(base: Path, config: DesktopConfig) -> None:
    """Tell the user how to finish setup in the browser (no default password)."""
    signup_url = config.console_url().rstrip("/") + "/get-started"
    welcome_path(base).write_text(
        "Welcome to Neuron!\n\n"
        "Your homeserver is running. To finish setup, open the link below and\n"
        "create your account — the first account you create becomes the server\n"
        "administrator, and you choose its password:\n\n"
        f"  {signup_url}\n\n"
        "Manage your server (users, rooms, invites) any time from the admin console —\n"
        "sign in there with the admin account you just created:\n\n"
        f"  {config.admin_console_url()}\n\n"
        "Then sign in from any Matrix client (Element, FluffyChat, …) with:\n"
        f"  Homeserver : {config.console_url()}\n"
        "  Your new username + password\n\n"
        f"All of your server's data lives in:\n  {base}\n",
        encoding="utf-8",
    )


def perform_first_run(
    base: Path,
    *,
    input_fn: InputFn = input,
    getpass_fn: InputFn = getpass,
    print_fn: PrintFn = print,
) -> DesktopConfig:
    """Run the interactive first-run flow: prompt, write config, create the admin."""
    config, password = run_interactive_setup(
        base, input_fn=input_fn, getpass_fn=getpass_fn, print_fn=print_fn
    )
    _finalize_first_run(base, config, password, print_fn=print_fn)
    return config


def default_first_run_config(base: Path) -> DesktopConfig:
    """The first-run config the GUI pre-fills (server name defaults to the hostname)."""
    return DesktopConfig(
        server_name=default_server_name(),
        data_dir=str(base),
        admin_username="admin",
        first_user_admin=True,
    )


def write_first_run_config(base: Path, config: DesktopConfig, *, print_fn: PrintFn = print) -> None:
    """Persist a chosen first-run ``config`` and write the welcome file."""
    _write_config(base, config)
    _write_welcome_file(base, config)
    print_fn(f"Configured Neuron in {base}.")
    print_fn(f"Finish setup at {config.console_url().rstrip('/')}/get-started")


def perform_noninteractive_first_run(base: Path, *, print_fn: PrintFn = print) -> DesktopConfig:
    """First-run setup with no prompts — for the double-clicked desktop app.

    There's no terminal to prompt on, so rather than invent a default password we
    configure the server to make the **first account that signs up** the admin, write
    a WELCOME.txt pointing at the in-browser sign-up, and let the user choose their own
    username and password there.
    """
    config = default_first_run_config(base)
    write_first_run_config(base, config, print_fn=print_fn)
    return config


def load_or_create(
    base: Path,
    *,
    input_fn: InputFn = input,
    getpass_fn: InputFn = getpass,
    print_fn: PrintFn = print,
) -> DesktopConfig:
    """Load an existing config, or run first-run setup if there isn't one.

    Uses interactive prompts when a terminal is available, otherwise falls back to
    non-interactive setup (so the GUI app can't crash on a missing ``sys.stdin``).
    """
    if is_first_run(base):
        if stdin_is_interactive():
            return perform_first_run(
                base, input_fn=input_fn, getpass_fn=getpass_fn, print_fn=print_fn
            )
        return perform_noninteractive_first_run(base, print_fn=print_fn)
    config = config_module.load(paths.config_path(base))
    # Mark that this app version has run here (so an upgrade isn't re-detected on the
    # next launch after the user has chosen to upgrade).
    record_version(base, current_app_version())
    return config
