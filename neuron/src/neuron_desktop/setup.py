# SPDX-License-Identifier: Apache-2.0
"""First-run setup: pick a server name, choose an admin, create the account.

The interactive prompts take injected ``input``/``getpass``/``print`` callables so
the flow is unit-testable without a real terminal. Account creation talks to the
same storage the server uses, so the admin can immediately sign in to the console.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from collections.abc import Callable
from getpass import getpass
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

    while True:
        password = getpass_fn("Admin password: ")
        confirm = getpass_fn("Confirm password: ")
        if password and password == confirm:
            break
        print_fn("Passwords were empty or did not match — please try again.")

    config = DesktopConfig(
        server_name=server_name, data_dir=str(base), admin_username=admin_username
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


def perform_noninteractive_first_run(base: Path, *, print_fn: PrintFn = print) -> DesktopConfig:
    """First-run setup with no prompts — for the double-clicked desktop app.

    There's no terminal to prompt on, so rather than invent a default password we
    configure the server to make the **first account that signs up** the admin, write
    a WELCOME.txt pointing at the in-browser sign-up, and let the user choose their own
    username and password there.
    """
    config = DesktopConfig(
        server_name=default_server_name(),
        data_dir=str(base),
        admin_username="admin",
        first_user_admin=True,
    )
    _write_config(base, config)
    _write_welcome_file(base, config)
    print_fn(f"Configured Neuron in {base}.")
    print_fn(f"Finish setup at {config.console_url().rstrip('/')}/get-started")
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
    return config_module.load(paths.config_path(base))
