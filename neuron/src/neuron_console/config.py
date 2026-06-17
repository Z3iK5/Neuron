"""Configuration for the admin console service.

Extends ``NeuronCoreSettings`` (which provides the Synapse connection + logging
settings) with console-specific settings: the operator login password and the
secret used to sign session cookies. All are read from ``NEURON_*`` env vars.
"""

from __future__ import annotations

import secrets

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
