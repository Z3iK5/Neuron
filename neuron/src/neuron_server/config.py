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

    # Whether open registration (POST /register) is allowed. Convenient for a
    # fresh MVP server so you can create the first account; gate this in
    # production (or front it with the admin API once HS-6 lands).
    registration_enabled: bool = Field(
        default=True,
        description="Allow open account registration via POST /_matrix/client/v3/register.",
    )

    # Bootstrap server admins: a comma-separated list of localparts or full user
    # IDs that are always treated as server admins (in addition to any user whose
    # stored admin flag is set). This is how you get the first admin so the Neuron
    # console / Admin API works. Example: NEURON_SERVER_ADMIN_USERS=admin,ops
    admin_users: str = Field(
        default="",
        description="Comma-separated localparts/user IDs treated as server admins.",
    )

    def admin_user_ids(self) -> set[str]:
        """Resolve ``admin_users`` to a set of full Matrix IDs."""
        result: set[str] = set()
        for raw in self.admin_users.split(","):
            entry = raw.strip()
            if not entry:
                continue
            result.add(entry if entry.startswith("@") else f"@{entry}:{self.name}")
        return result

    # --- Media repository ---------------------------------------------------
    # Directory where uploaded media blobs are stored (filesystem backend).
    media_store_path: str = Field(
        default="./neuron-media",
        description="Filesystem directory for stored media blobs.",
    )
    # Maximum accepted upload size, in bytes (default 50 MiB).
    max_upload_bytes: int = Field(
        default=50 * 1024 * 1024,
        gt=0,
        description="Maximum media upload size in bytes.",
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
