# SPDX-License-Identifier: Apache-2.0
"""Run the homeserver: ``python -m neuron_server`` (or the ``neuron-server`` script).

Subcommands:

- ``serve`` (the default) — load settings from the environment (``NEURON_SERVER_*``),
  build the app, and serve it with uvicorn on the configured host/port.
- ``doctor`` — run a preflight / health check over the configuration, database,
  signing key, media store and (unless ``--offline``) network reachability, then
  exit non-zero if anything is broken (``--strict`` also fails on warnings).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

import uvicorn

from neuron_core import configure_logging
from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.doctor import doctor_main


def _serve(settings: NeuronServerSettings) -> None:
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    app = create_app(settings)
    # ``log_config=None``: keep our own logging (configured above) and stop uvicorn
    # installing its default config, whose colourised formatter probes
    # ``sys.stdout.isatty()`` and crashes when stdout is None (windowed frozen app).
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port, log_config=None)


def _doctor(settings: NeuronServerSettings, *, offline: bool, strict: bool) -> int:
    # Keep the report readable: log warnings/errors only, not INFO chatter.
    configure_logging(level="WARNING", fmt="console")
    return asyncio.run(doctor_main(settings, offline=offline, strict=strict))


def main(argv: Sequence[str] | None = None) -> None:
    # ``argv`` defaults to the process args, but the desktop app calls this with an
    # explicit list (e.g. ``[]``) when it re-execs the frozen bundle as its server
    # child — so the internal ``_server`` token never leaks into this parser.
    parser = argparse.ArgumentParser(prog="neuron-server", description="The Neuron homeserver.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the homeserver (default).")
    doctor = sub.add_parser("doctor", help="Check configuration and reachability.")
    doctor.add_argument(
        "--offline", action="store_true", help="Skip network checks (config only)."
    )
    doctor.add_argument(
        "--strict", action="store_true", help="Exit non-zero on warnings too."
    )

    args = parser.parse_args(argv)
    settings = NeuronServerSettings()

    if args.command == "doctor":
        raise SystemExit(_doctor(settings, offline=args.offline, strict=args.strict))
    _serve(settings)


if __name__ == "__main__":
    main()
