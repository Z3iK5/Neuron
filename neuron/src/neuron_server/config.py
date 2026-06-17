# SPDX-License-Identifier: Apache-2.0
"""Configuration for ``neuron_server``.

Read from environment variables prefixed with ``NEURON_SERVER_`` (validated at
startup via ``pydantic-settings``). For example ``name`` is read from
``NEURON_SERVER_NAME`` — the homeserver's own server name, the same value the
Neuron tooling uses to build Matrix IDs.

Secrets never live in the repository: pass them via the environment (or a
git-ignored ``.env`` for local dev).
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NeuronServerSettings(BaseSettings):
    """Runtime settings for the homeserver."""

    model_config = SettingsConfigDict(
        env_prefix="NEURON_SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The homeserver's server name — the domain part of every Matrix ID it owns
    # (e.g. "example.org" -> @alice:example.org). For local dev "neuron.local"
    # is fine. This is the server's permanent identity; it must not change once a
    # database has been initialized (the server refuses to start if it does).
    name: str = Field(
        default="neuron.local",
        description="The homeserver's server name (domain part of Matrix IDs).",
    )

    # The public base URL clients use to reach this server's Client-Server API.
    # Advertised via /.well-known/matrix/client for client auto-discovery.
    public_base_url: str = Field(
        default="http://localhost:8008",
        description="Public base URL of the Client-Server API (used in .well-known).",
    )

    # Async database URL. SQLite for development, PostgreSQL for production:
    #   sqlite:///./neuron_server.db   (relative file)
    #   sqlite:///:memory:             (ephemeral, dev/tests)
    #   postgresql://user:pass@host/db
    database_url: str = Field(
        default="sqlite:///./neuron_server.db",
        description="Async database URL (sqlite:///... or postgresql://...).",
    )

    # Where the ASGI server binds when run via `python -m neuron_server`.
    bind_host: str = Field(default="127.0.0.1", description="ASGI bind host.")
    bind_port: int = Field(default=8008, gt=0, description="ASGI bind port.")

    # --- Logging ------------------------------------------------------------
    log_level: str = Field(default="INFO", description="Python log level name.")
    log_format: str = Field(
        default="json",
        description="Log output format: 'json' (machine-readable) or 'console' (human).",
    )
