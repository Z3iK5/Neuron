# SPDX-License-Identifier: Apache-2.0
"""Configuration for the audit bot."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from neuron_core.config import NeuronCoreSettings


class AuditorSettings(NeuronCoreSettings):
    """Settings for neuron-auditor (inherits Synapse connection + logging)."""

    model_config = SettingsConfigDict(
        env_prefix="NEURON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The audit bot's own access token (Client-Server API).
    auditor_bot_token: SecretStr = Field(
        default=SecretStr(""), description="Access token for the audit bot account."
    )

    # Automatically accept room invites so the bot starts auditing new rooms.
    auditor_auto_join: bool = True

    # Optional path to a JSON file of Megolm session keys to import at startup
    # (enables decrypting E2EE rooms whose keys are provided). Requires the 'e2e'
    # extra (libolm). Such a file can decrypt messages — protect it accordingly.
    auditor_e2e_key_file: str = ""

    # Path to persist the bot's Olm device (account + Olm sessions). When set, the
    # bot publishes device + one-time keys on startup and automatically ingests
    # room keys it is sent via to-device messages — full E2EE. Requires 'e2e'.
    # This file is the device's secret identity — protect it.
    auditor_e2e_device_store: str = ""

    # How many one-time keys to publish on startup (so senders can establish Olm
    # sessions to share room keys with the bot).
    auditor_e2e_one_time_keys: int = 50

    # Set up cross-signing for the bot (publish master/self-signing/user-signing
    # keys and self-sign the device) so it presents a verifiable identity. The
    # upload usually needs interactive auth — see the README. Seeds are persisted
    # next to the device store and are highly sensitive.
    auditor_e2e_cross_signing: bool = False

    # Where to persist the /sync pagination token (so a restart resumes without
    # gaps or duplicates). Relative paths are fine for local dev.
    auditor_state_path: str = "auditor-state.json"

    # Sink selection: "file", "s3", or "both".
    auditor_sink: str = "file"

    # Filesystem sink: the JSON Lines file events are appended to.
    auditor_file_path: str = "audit-log.jsonl"

    # S3 sink (S3-compatible; e.g. AWS S3 or MinIO in dev).
    auditor_s3_endpoint_url: str = ""   # e.g. http://localhost:9000 for MinIO; blank = AWS
    auditor_s3_bucket: str = ""
    auditor_s3_prefix: str = "audit"
    auditor_s3_access_key: SecretStr = SecretStr("")
    auditor_s3_secret_key: SecretStr = SecretStr("")
    auditor_s3_region: str = "us-east-1"

    def has_bot_token(self) -> bool:
        return bool(self.auditor_bot_token.get_secret_value())
