"""Command-line runner for the audit bot.

Usage (with the environment configured — see neuron/README.md)::

    python -m neuron_auditor run     # stream events to the sink until stopped
    python -m neuron_auditor once    # do a single sync + record, then exit

Requires NEURON_AUDITOR_BOT_TOKEN and a sink configuration (file by default).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from neuron_auditor.config import AuditorSettings
from neuron_auditor.core import Auditor
from neuron_auditor.sinks import make_sink
from neuron_auditor.state import StateStore
from neuron_core import MatrixClient, configure_logging, get_logger

log = get_logger("neuron_auditor")


async def _amain(command: str) -> None:
    settings = AuditorSettings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    if not settings.has_bot_token():
        raise SystemExit("NEURON_AUDITOR_BOT_TOKEN is required.")

    client = MatrixClient(
        settings.synapse_base_url,
        settings.auditor_bot_token.get_secret_value(),
        timeout=settings.http_timeout_seconds,
    )
    auditor = Auditor(
        client,
        make_sink(settings),
        StateStore(settings.auditor_state_path),
        auto_join=settings.auditor_auto_join,
    )
    try:
        if command == "once":
            count = await auditor.poll_once()
            log.info("single poll complete", extra={"count": count})
        else:
            await auditor.run_forever()
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="neuron_auditor")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "once"],
        help="'run' = stream until stopped; 'once' = a single sync + record.",
    )
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(args.command))


if __name__ == "__main__":
    main()
