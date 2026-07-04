# SPDX-License-Identifier: Apache-2.0
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
from typing import TYPE_CHECKING

from neuron_auditor.config import AuditorSettings
from neuron_auditor.core import Auditor
from neuron_auditor.sinks import make_sink
from neuron_auditor.state import StateStore
from neuron_core import MatrixClient, configure_logging, get_logger
from neuron_core.errors import MatrixError
from neuron_crypto.base import Decryptor

if TYPE_CHECKING:
    from neuron_crypto.olm_device import OlmDevice

log = get_logger("neuron_auditor")


async def _setup_cross_signing(
    client: MatrixClient, device: OlmDevice, user_id: str, path: str
) -> None:
    """Publish the bot's cross-signing keys and self-sign its device.

    Uploading cross-signing keys usually needs interactive auth (UIA); if the
    server rejects it we log a warning and carry on (the operator can verify the
    bot device manually) rather than failing the whole run.
    """
    from neuron_crypto.cross_signing import CrossSigning

    cross = CrossSigning.load(path, user_id) or CrossSigning(user_id)
    cross.save(path)
    try:
        await client.upload_cross_signing_keys(cross.device_signing_upload())
        await client.upload_signatures(cross.sign_device(device.device_keys()))
        log.info("cross-signing keys published")
    except MatrixError as exc:
        log.warning(
            "cross-signing upload failed (likely needs interactive auth); "
            "verify the bot device manually",
            extra={"status": exc.status_code, "errcode": exc.errcode},
        )


async def _build_decryptor(client: MatrixClient, settings: AuditorSettings) -> Decryptor | None:
    """Construct the right decryptor for the configured E2EE mode (or None)."""
    if settings.auditor_e2e_device_store:
        # Full E2EE: a persistent Olm device that publishes keys and auto-ingests
        # room keys sent to it via to-device messages.
        from neuron_crypto.manager import E2EEManager
        from neuron_crypto.megolm import MegolmSessionStore
        from neuron_crypto.olm_device import OlmDevice

        device_path = settings.auditor_e2e_device_store
        store_path = f"{device_path}.sessions.json"

        whoami = await client.whoami()
        user_id = whoami.get("user_id", "")
        device_id = whoami.get("device_id") or "NEURON_AUDITOR"
        device = OlmDevice.load(device_path, user_id, device_id) or OlmDevice(user_id, device_id)

        one_time_keys = device.generate_one_time_keys(settings.auditor_e2e_one_time_keys)
        await client.keys_upload(device_keys=device.device_keys(), one_time_keys=one_time_keys)
        device.save(device_path)

        if settings.auditor_e2e_cross_signing:
            await _setup_cross_signing(client, device, user_id, f"{device_path}.crosssigning.json")

        store = MegolmSessionStore()
        store.load(store_path)
        if settings.auditor_e2e_key_file:
            store.import_key_file(settings.auditor_e2e_key_file)
        log.info("e2ee enabled (automatic key receipt)", extra={"device_id": device_id})
        return E2EEManager(
            device,
            store,
            device_path=device_path,
            store_path=store_path,
            otk_target=settings.auditor_e2e_one_time_keys,
        )

    if settings.auditor_e2e_key_file:
        # Import-only: decrypt rooms whose keys are provided in a key file.
        from neuron_crypto.megolm import MegolmDecryptor, MegolmSessionStore

        store = MegolmSessionStore()
        imported = store.import_key_file(settings.auditor_e2e_key_file)
        log.info("imported room keys", extra={"count": imported})
        return MegolmDecryptor(store)

    return None


async def _amain(command: str) -> None:
    settings = AuditorSettings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    if not settings.has_bot_token():
        raise SystemExit("NEURON_AUDITOR_BOT_TOKEN is required.")

    client = MatrixClient(
        settings.homeserver_url,
        settings.auditor_bot_token.get_secret_value(),
        timeout=settings.http_timeout_seconds,
    )

    # E2EE (needs the 'e2e' extra + libolm). Two modes:
    #  - device store set -> full E2EE: publish keys + auto-receive room keys.
    #  - only a key file   -> decrypt rooms whose keys were imported manually.
    decryptor = await _build_decryptor(client, settings)

    auditor = Auditor(
        client,
        make_sink(settings),
        StateStore(settings.auditor_state_path),
        decryptor=decryptor,
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
