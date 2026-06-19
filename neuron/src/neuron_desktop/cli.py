# SPDX-License-Identifier: Apache-2.0
"""The ``neuron-desktop`` command line.

Subcommands:

* (default) / ``run`` — first-run setup if needed, then start the server;
* ``setup`` — (re)run the first-run setup only;
* ``where`` — print the data directory and config path;
* ``console`` — open the admin console in a browser.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from neuron_desktop import config as config_module
from neuron_desktop import paths, setup, supervisor

# Internal command used when the (possibly frozen) app re-execs itself to run the
# homeserver child process. Not part of the public CLI surface.
_SERVER_COMMAND = "_server"


def _reveal(path: Path) -> None:
    """Best-effort: open a file in the OS default handler. Never raises."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def _configured(base: Path) -> config_module.DesktopConfig:
    """Ensure the app is configured (first-run setup if needed).

    After a non-interactive first run (the double-clicked app), reveal the
    WELCOME.txt so the user can find their auto-created admin credentials.
    """
    fresh = setup.is_first_run(base)
    config = setup.load_or_create(base)
    if fresh and setup.welcome_path(base).exists():
        _reveal(setup.welcome_path(base))
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="neuron-desktop", description="Run a Neuron homeserver.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Start the server (running first-run setup if needed).")
    sub.add_parser("setup", help="Run the first-run setup wizard.")
    sub.add_parser("where", help="Print the data directory and config path.")
    sub.add_parser("console", help="Open the admin console in a browser.")
    sub.add_parser("tray", help="Run the menu-bar / system-tray app (needs a desktop).")
    sub.add_parser("settings", help="Open the native settings window (needs a desktop).")
    return parser


def _open_settings_window(base: Path) -> int:
    """Open the native settings window for the current (or default) config."""
    from neuron_desktop import settings_window

    first = setup.is_first_run(base)
    current = setup.default_first_run_config(base) if first else config_module.load(
        paths.config_path(base)
    )
    try:
        updated = settings_window.open_settings_window(current, first_run=first)
    except Exception as exc:  # no display / tkinter unavailable
        print(f"Settings window unavailable: {exc}")
        return 1
    if updated is None:
        return 0
    if first:
        setup.write_first_run_config(base, updated)
    else:
        config_module.save(updated, paths.config_path(base))
    print("Settings saved.")
    return 0


def _first_run_choose_name(base: Path) -> None:
    """On first run, let the user name the server in the native window before start.

    Falls back silently to the non-interactive default (hostname) when there's no
    display or the user cancels — `_configured` then performs that default setup.
    """
    try:
        from neuron_desktop import settings_window

        chosen = settings_window.open_settings_window(
            setup.default_first_run_config(base), first_run=True
        )
    except Exception as exc:  # no display / tkinter unavailable
        print(f"Settings window unavailable ({exc}); using defaults.")
        return
    if chosen is not None:
        setup.write_first_run_config(base, chosen)
        if setup.welcome_path(base).exists():
            _reveal(setup.welcome_path(base))


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)

    # The homeserver child process re-execs this app as ``<app> _server`` and the
    # server reads its NEURON_SERVER_* settings from the environment.
    if args_list and args_list[0] == _SERVER_COMMAND:
        from neuron_server.__main__ import main as run_server

        # Pass an explicit empty argv so neuron_server's own parser doesn't try to
        # interpret our internal "_server" token (it only knows serve/doctor) —
        # with no args it takes the default "serve" path.
        run_server([])
        return 0

    args = build_parser().parse_args(args_list)
    base = paths.data_dir()

    if args.command == "where":
        print(f"data directory: {base}")
        print(f"config file:    {paths.config_path(base)}")
        return 0

    if args.command == "setup":
        setup.perform_first_run(base)
        return 0

    if args.command == "console":
        if setup.is_first_run(base):
            print("No server configured yet — run 'neuron-desktop setup' first.")
            return 1
        url = supervisor.open_console(config_module.load(paths.config_path(base)))
        print(f"Opening {url}")
        return 0

    if args.command == "settings":
        return _open_settings_window(base)

    if args.command == "tray":
        if setup.is_first_run(base):
            _first_run_choose_name(base)
        config = _configured(base)
        try:
            from neuron_desktop import tray

            tray.run_tray(config)
        except (SystemExit, Exception) as exc:  # tray/GUI backend unavailable
            # Don't die silently — keep the homeserver running so the user can
            # still reach it in a browser (the console URL is in WELCOME.txt).
            print(f"Tray unavailable ({exc}); running the server in the foreground.")
            supervisor.serve(config)
        return 0

    # Default / "run": ensure configured, then serve.
    supervisor.serve(_configured(base))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
