# SPDX-License-Identifier: Apache-2.0
"""Configuration for the supervision bot."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from neuron_core.config import NeuronCoreSettings


class SupervisorSettings(NeuronCoreSettings):
    """Settings for neuron-supervisor (inherits homeserver connection + logging)."""

    model_config = SettingsConfigDict(
        env_prefix="NEURON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The bot's full Matrix ID, e.g. "@supervisor:example.org". Must be a LOCAL
    # account on the homeserver (make_room_admin only works for local users).
    supervisor_bot_user_id: str = Field(
        default="", description="Full Matrix ID of the supervision bot account."
    )

    # The bot account's own access token (Client-Server API), used for kick/ban
    # and event redaction. Obtain it by logging the bot in, or via the admin
    # 'login as user' endpoint (classic auth only).
    supervisor_bot_token: SecretStr = Field(
        default=SecretStr(""), description="Access token for the supervision bot account."
    )

    # How often (seconds) the background loop re-scans rooms to keep the bot
    # promoted. Detection of new rooms is poll-based in this phase.
    supervisor_poll_interval_seconds: float = Field(default=60.0, gt=0)

    def has_bot_token(self) -> bool:
        return bool(self.supervisor_bot_token.get_secret_value())
