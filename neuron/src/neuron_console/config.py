# SPDX-License-Identifier: Apache-2.0
"""Configuration for the admin console service.

Extends ``NeuronCoreSettings`` (which provides the homeserver connection +
logging settings) with console-specific settings: the operator login password and the
secret used to sign session cookies. All are read from ``NEURON_*`` env vars.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from neuron_core.config import NeuronCoreSettings


class ConsoleSettings(NeuronCoreSettings):
    """Settings for neuron-console."""

    model_config = SettingsConfigDict(
        env_prefix="NEURON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The password an operator types to log in to the console. REQUIRED in
    # practice; defaults to empty so tests can set it explicitly.
    console_password: SecretStr = SecretStr("")

    # Secret used to sign the session cookie. If left empty we generate a random
    # one at startup (fine for local dev — sessions just won't survive a restart).
    # In production set NEURON_CONSOLE_SESSION_SECRET to a stable random value.
    console_session_secret: SecretStr = SecretStr("")

    # Cookie name for the session.
    session_cookie_name: str = "neuron_console_session"

    # The homeserver's public base URL — the address end users reach (used to build
    # shareable invite links). Often the same as the URL the console uses to reach
    # the homeserver, but split out for deployments where they differ (e.g. the
    # console talks to the homeserver over a private network). Falls back to
    # ``homeserver_url`` when unset.
    homeserver_public_url: str = ""

    def public_base_url(self) -> str:
        """Public base URL for end users, used to build invite links (no trailing /)."""
        base = self.homeserver_public_url or self.homeserver_url
        return base.rstrip("/")

    # --- Passkeys (WebAuthn) for console login ------------------------------
    # The console keeps a little state (the registered-passkeys file) under this
    # directory. Defaults to ~/.neuron-console; override with NEURON_CONSOLE_DATA_DIR.
    console_data_dir: str = ""
    # WebAuthn relying-party id + origin. Leave empty to derive from the request
    # (correct for localhost / direct access); set them when behind a reverse proxy
    # so they match the address in the browser (e.g. rp id "chat.example.org",
    # origin "https://chat.example.org").
    webauthn_rp_id: str = ""
    webauthn_origin: str = ""

    def passkey_store_path(self) -> Path:
        """Where the registered-passkeys JSON file lives."""
        base = (
            Path(self.console_data_dir).expanduser()
            if self.console_data_dir
            else Path.home() / ".neuron-console"
        )
        return base / "passkeys.json"

    # Optional supervision-bot wiring (Phase 3). If a bot token is set, the
    # console's Supervision tab can kick/ban as the bot; promotion only needs the
    # bot's user ID + the server-admin token.
    supervisor_bot_user_id: str = ""
    supervisor_bot_token: SecretStr = SecretStr("")

    def has_supervisor_bot(self) -> bool:
        """True if a bot access token is configured (enables kick/ban)."""
        return bool(self.supervisor_bot_token.get_secret_value())

    def effective_session_secret(self) -> str:
        """Return the configured session secret, or a random one for dev."""
        configured = self.console_session_secret.get_secret_value()
        return configured or secrets.token_urlsafe(32)

    def check_password(self, candidate: str) -> bool:
        """Constant-time comparison of a submitted password to the configured one."""
        expected = self.console_password.get_secret_value()
        if not expected:
            # No password configured → refuse all logins rather than allow all.
            return False
        return secrets.compare_digest(candidate, expected)
