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

import secrets

from pydantic import Field, SecretStr
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
    # PostgreSQL connection-pool size (ignored for SQLite). Raising it above 1 is
    # now SAFE within a single process: the multi-writer position tracker means
    # /sync uses a contiguous "persisted upto" floor instead of MAX(col), so an id
    # committed out of allocation order across connections is never skipped. (Note:
    # running multiple worker PROCESSES — distinct instance_names — is loss-free but
    # still needs the position heartbeat before an idle instance stops holding the
    # floor back; until then run a single process.)
    db_pool_size: int = Field(
        default=1, gt=0, description="PostgreSQL connection pool size (SQLite ignores this)."
    )
    # How ``/sync`` long-polls are woken across worker processes. ``auto`` (the
    # default) stays in-process for SQLite and uses Postgres LISTEN/NOTIFY for a
    # postgresql:// URL — so a wake on one worker reaches syncs parked on another.
    # ``inprocess`` forces the single-process notifier; ``pg`` requires Postgres;
    # ``redis`` is reserved for a future Redis transport.
    notifier_backend: str = Field(
        default="auto",
        description="Cross-worker /sync wake backend: auto | inprocess | pg | redis.",
    )
    # Stable per-process identity for multi-writer stream positions (Postgres).
    # Each worker records its contiguous "persisted upto" position per stream under
    # this name; the /sync floor is the minimum across instances. Must be stable
    # across restarts (a changing name orphans the old row and holds the floor
    # back). Irrelevant to SQLite (single process). Default "master".
    instance_name: str = Field(
        default="master",
        description="Stable worker identity for multi-writer stream positions.",
    )

    # Whether open registration (POST /register) is allowed. Convenient for a
    # fresh MVP server so you can create the first account; gate this in
    # production (or front it with the admin API once HS-6 lands).
    registration_enabled: bool = Field(
        default=True,
        description="Allow open account registration via POST /_matrix/client/v3/register.",
    )

    # Grant server-admin to the first account that registers. The desktop first-run
    # flow turns this on so the user who signs up in the browser owns the server,
    # with no pre-created default admin/password.
    first_user_admin: bool = Field(
        default=False,
        description="Make the first account that registers a server admin.",
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

    # --- Federation identity (HS-7) ----------------------------------------
    # Optional path to the server's Ed25519 signing key (Synapse-compatible
    # ``ed25519 <version> <base64-seed>`` format). If set, the key is loaded from
    # there (created on first run); if empty, it is generated once and persisted
    # in the database. This key is the server's federation identity — back it up.
    signing_key_path: str = Field(
        default="",
        description="Path to the Ed25519 signing key file (else stored in the DB).",
    )
    # How long (ms) other servers may cache our published /_matrix/key/v2/server
    # response before refetching. Default 7 days.
    key_validity_period_ms: int = Field(
        default=7 * 24 * 60 * 60 * 1000,
        gt=0,
        description="valid_until_ts horizon for the published server key, in ms.",
    )
    # How often (seconds) the background flusher retries undelivered federation
    # transactions to destinations that were offline.
    federation_retry_interval_s: float = Field(
        default=30.0,
        gt=0,
        description="Interval for retrying queued outbound federation transactions.",
    )
    # Route inbound-federation authorization through state resolution v2. Off by
    # default: the linear single-extremity model makes it a no-op today, but it
    # keeps the (already-tested) algorithm on the live path and is the seam for
    # real multi-extremity resolution once forward extremities exist.
    state_res_v2: bool = Field(
        default=False,
        description="Use state resolution v2 for inbound federation authorization.",
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

    # --- Admin console ------------------------------------------------------
    # Secret used to sign the admin-console session cookie. If empty, a random one
    # is generated at startup (fine for a single desktop server — sessions just
    # won't survive a restart). Set NEURON_SERVER_CONSOLE_SESSION_SECRET to a stable
    # random value to keep operators logged in across restarts.
    console_session_secret: SecretStr = Field(
        default=SecretStr(""),
        description="Secret signing key for the admin-console session cookie.",
    )
    # Name of the admin-console session cookie.
    session_cookie_name: str = Field(
        default="neuron_session",
        description="Cookie name for the admin-console session.",
    )
    # Path to the desktop app's config.json, when run by neuron_desktop. If set, the
    # console settings page can edit the persisted runtime settings (applied on the
    # next server restart). Empty when running as a standalone server.
    desktop_config_path: str = Field(
        default="",
        description="Path to the desktop config.json (enables console settings editing).",
    )

    # WebAuthn relying-party id + origin for console passkeys. Leave empty to derive
    # from the request (rp_id = hostname, origin = scheme://host:port) — correct for
    # the desktop's localhost. Set them when serving the console behind a domain.
    webauthn_rp_id: str = Field(
        default="", description="WebAuthn relying-party id (else derived per-request)."
    )
    webauthn_origin: str = Field(
        default="", description="WebAuthn expected origin (else derived per-request)."
    )

    def effective_session_secret(self) -> str:
        """Return the configured console session secret, or a random dev one."""
        configured = self.console_session_secret.get_secret_value()
        return configured or secrets.token_urlsafe(32)
