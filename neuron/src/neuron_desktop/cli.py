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
from collections.abc import Sequence

from neuron_desktop import config as config_module
from neuron_desktop import paths, setup, supervisor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="neuron-desktop", description="Run a Neuron homeserver.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Start the server (running first-run setup if needed).")
    sub.add_parser("setup", help="Run the first-run setup wizard.")
    sub.add_parser("where", help="Print the data directory and config path.")
    sub.add_parser("console", help="Open the admin console in a browser.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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

    # Default / "run": ensure configured, then serve.
    config = setup.load_or_create(base)
    supervisor.serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
