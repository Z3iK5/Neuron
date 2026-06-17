# SPDX-License-Identifier: Apache-2.0
"""Configuration loading for Neuron services.

We use ``pydantic-settings`` so configuration can come from **environment
variables** (the recommended way to pass secrets) and is **validated** at
startup — if a required value is missing or malformed, the service fails fast
with a clear message instead of misbehaving later.

Every setting here is prefixed with ``NEURON_`` in the environment. For example
``homeserver_url`` is read from the ``NEURON_HOMESERVER_URL`` env var.

Secrets (like the admin token) use ``SecretStr`` so they are not accidentally
printed in logs or tracebacks.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class NeuronCoreSettings(BaseSettings):
    """Settings shared by all Neuron services.

    Individual services will subclass this to add their own fields while
    inheriting the homeserver-connection and logging settings.
    """

    model_config = SettingsConfigDict(
        env_prefix="NEURON_",
        env_file=".env",        # convenient for local development; ignored if absent
        env_file_encoding="utf-8",
        extra="ignore",         # ignore unrelated env vars rather than erroring
    )

    # --- Homeserver connection ----------------------------------------------
    # Base URL of the backend homeserver, e.g. "http://localhost:8008" in dev or
    # "https://matrix.example.org" in production. No trailing slash needed.
    homeserver_url: str = Field(
        default="http://localhost:8008",
        description="Base URL of the homeserver to talk to.",
    )

    # A server-admin access token. REQUIRED for services that use the Admin API.
    # Kept as a SecretStr so it never leaks into logs. In production this is
    # injected via the environment / a mounted secret, never committed.
    homeserver_admin_token: SecretStr = Field(
        default=SecretStr(""),
        description="Server-admin access token used for the homeserver Admin API.",
    )

    # How long (seconds) to wait on a single HTTP request to the homeserver.
    http_timeout_seconds: float = Field(default=30.0, gt=0)

    # The homeserver's server name (the part after the colon in a Matrix ID,
    # e.g. "example.org"). Used to build full user IDs from a localpart. Optional.
    server_name: str = Field(
        default="",
        description="Homeserver name used to build Matrix IDs (e.g. 'example.org').",
    )

    # Authentication mode of the target homeserver:
    #   "classic" — the homeserver handles passwords/login itself (default).
    #   "mas"      — auth is delegated to Matrix Authentication Service (MSC3861),
    #                which DISABLES several admin endpoints (password reset,
    #                admin-flag, login-as-user). Services use this to behave correctly.
    auth_mode: str = Field(default="classic", description="'classic' or 'mas'.")

    # --- Logging ------------------------------------------------------------
    log_level: str = Field(default="INFO", description="Python log level name.")
    log_format: str = Field(
        default="json",
        description="Log output format: 'json' (machine-readable) or 'console' (human).",
    )

    def has_admin_token(self) -> bool:
        """Return True if a non-empty admin token has been configured."""
        return bool(self.homeserver_admin_token.get_secret_value())

    def mas_enabled(self) -> bool:
        """Return True if the homeserver delegates auth to MAS (MSC3861)."""
        return self.auth_mode.lower() == "mas"

    def build_user_id(self, localpart: str) -> str:
        """Build a full Matrix ID from a localpart using the configured server name.

        Accepts an already-full ID (starting with ``@``) unchanged.
        """
        if localpart.startswith("@"):
            return localpart
        if not self.server_name:
            raise ValueError(
                "NEURON_SERVER_NAME must be set to build a user ID from a localpart."
            )
        return f"@{localpart}:{self.server_name}"
