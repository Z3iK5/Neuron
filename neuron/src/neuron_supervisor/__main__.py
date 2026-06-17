"""Command-line runner for the supervision bot.

Usage (with the environment configured — see neuron/README.md)::

    python -m neuron_supervisor sync   # promote the bot in all rooms once, then exit
    python -m neuron_supervisor run    # do that repeatedly on a timer (poll loop)

Detection of new rooms is poll-based in this phase: the loop simply re-runs
``ensure_admin_in_all_rooms`` every ``NEURON_SUPERVISOR_POLL_INTERVAL_SECONDS``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from neuron_core import MatrixClient, SynapseAdminClient, configure_logging, get_logger
from neuron_supervisor.config import SupervisorSettings
from neuron_supervisor.core import Supervisor

log = get_logger("neuron_supervisor")


def _build_supervisor(
    settings: SupervisorSettings,
) -> tuple[Supervisor, SynapseAdminClient, MatrixClient | None]:
    admin = SynapseAdminClient(
        settings.synapse_base_url,
        settings.synapse_admin_token.get_secret_value(),
        timeout=settings.http_timeout_seconds,
    )
    bot: MatrixClient | None = None
    if settings.has_bot_token():
        bot = MatrixClient(
            settings.synapse_base_url,
            settings.supervisor_bot_token.get_secret_value(),
            timeout=settings.http_timeout_seconds,
        )
    supervisor = Supervisor(admin, settings.supervisor_bot_user_id, bot=bot)
    return supervisor, admin, bot


async def _run_once(supervisor: Supervisor) -> None:
    results = await supervisor.ensure_admin_in_all_rooms()
    promoted = sum(1 for r in results if r["promoted"])
    log.info("promotion sweep complete", extra={"rooms": len(results), "promoted": promoted})


async def _amain(command: str) -> None:
    settings = SupervisorSettings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    supervisor, admin, bot = _build_supervisor(settings)
    try:
        if command == "sync":
            await _run_once(supervisor)
        else:  # "run" — poll loop
            log.info(
                "supervisor loop starting",
                extra={"interval_s": settings.supervisor_poll_interval_seconds},
            )
            while True:
                await _run_once(supervisor)
                await asyncio.sleep(settings.supervisor_poll_interval_seconds)
    finally:
        await admin.aclose()
        if bot is not None:
            await bot.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="neuron_supervisor")
    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["sync", "run"],
        help="'sync' = promote once and exit; 'run' = poll loop.",
    )
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(args.command))


if __name__ == "__main__":
    main()
