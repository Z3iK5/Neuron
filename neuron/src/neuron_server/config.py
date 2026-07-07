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
    # ``inprocess`` forces the single-process notifier; ``pg`` requires Postgres.
    notifier_backend: str = Field(
        default="auto",
        description="Cross-worker /sync wake backend: auto | inprocess | pg.",
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

    # --- Rate limiting ------------------------------------------------------
    # In-process token-bucket limits on abuse-prone endpoints. Account-keyed limits
    # (login-by-account, message) work behind a proxy with no extra config; the
    # IP-keyed limits (login-by-IP, registration) need the real client address, so
    # set NEURON_SERVER_TRUSTED_PROXIES when behind a reverse proxy (else every
    # request appears to come from the proxy and shares one bucket). Enabled by
    # default with generous bursts so normal use is unaffected; tune per deployment.
    rate_limit_enabled: bool = Field(
        default=True, description="Enable request rate limiting."
    )
    # Password login, keyed by the account being logged into (brute-force defence).
    rate_limit_login_hz: float = Field(
        default=0.17, gt=0, description="Sustained login attempts/sec per account."
    )
    rate_limit_login_burst: int = Field(
        default=5, gt=0, description="Burst of login attempts allowed per account."
    )
    # Password login, keyed by client IP — catches one host spraying many accounts
    # (which the per-account limit above misses). Looser than per-account.
    rate_limit_login_ip_hz: float = Field(
        default=0.5, gt=0, description="Sustained login attempts/sec per client IP."
    )
    rate_limit_login_ip_burst: int = Field(
        default=15, gt=0, description="Burst of login attempts allowed per client IP."
    )
    # Account registration, keyed by client IP (sign-up spam / mass-account defence).
    rate_limit_registration_hz: float = Field(
        default=0.03, gt=0, description="Sustained registrations/sec per client IP."
    )
    rate_limit_registration_burst: int = Field(
        default=5, gt=0, description="Burst of registrations allowed per client IP."
    )
    # Message sending, keyed by the sender (spam defence).
    rate_limit_message_hz: float = Field(
        default=0.5, gt=0, description="Sustained messages/sec per user."
    )
    rate_limit_message_burst: int = Field(
        default=20, gt=0, description="Burst of messages allowed per user."
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

    # How long (seconds) an open User-Interactive-Auth session (e.g. an in-progress
    # registration) stays valid before a background sweep removes it. Sessions are
    # stored in the database so the challenge and retry can hit different workers;
    # this only bounds how long an abandoned challenge lingers. Default 1 hour.
    uia_session_ttl_s: float = Field(
        default=3600.0, gt=0, description="TTL (seconds) for User-Interactive-Auth sessions."
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
    # Maximum size (bytes) of a single remote media file fetched over federation and
    # cached locally (default 100 MiB). The cap is enforced while streaming, before
    # anything is stored, so an oversized or lying origin can't fill our disk. Set it
    # at or above ``max_upload_bytes`` so media a peer accepted can still be mirrored.
    max_remote_media_bytes: int = Field(
        default=100 * 1024 * 1024,
        gt=0,
        description="Maximum size in bytes of remote media fetched+cached over federation.",
    )
    # Media blob backend: 'filesystem' (the default — a local directory, right for
    # the desktop / a single host) or 's3' (an S3-compatible bucket, so multiple
    # workers/hosts share media — required for multi-host scale-out). For 's3',
    # credentials come from the standard AWS chain (AWS_ACCESS_KEY_ID/... env or an
    # instance role), never from here.
    media_backend: str = Field(
        default="filesystem",
        description="Media blob backend: filesystem | s3.",
    )
    s3_media_bucket: str = Field(
        default="", description="S3 bucket for media (required when media_backend=s3)."
    )
    # Optional: set for S3-compatible stores (MinIO, Cloudflare R2, ...); empty = AWS S3.
    s3_media_endpoint_url: str = Field(
        default="", description="Custom S3 endpoint URL (for S3-compatible stores)."
    )
    s3_media_region: str = Field(default="", description="S3 region (optional).")
    s3_media_prefix: str = Field(
        default="", description="Optional key prefix for media objects in the bucket."
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
    # How often (seconds) each Postgres worker re-publishes its stream positions so
    # an idle instance advances to the committed max and stops holding the shared
    # /sync floor back. Only runs on Postgres with multiple worker processes.
    position_heartbeat_interval_s: float = Field(
        default=30.0,
        gt=0,
        description="Interval for the multi-writer stream-position heartbeat (Postgres).",
    )
    # Route inbound-federation authorization through state resolution v2. Off by
    # default: the linear single-extremity model makes it a no-op today, but it
    # keeps the (already-tested) algorithm on the live path and is the seam for
    # real multi-extremity resolution once forward extremities exist.
    state_res_v2: bool = Field(
        default=False,
        description="Use state resolution v2 for inbound federation authorization.",
    )

    # --- Push notifications --------------------------------------------------
    # HTTP timeout (seconds) for a POST to a mobile push gateway (Sygnal). Delivery
    # runs off the request path as a best-effort background task, so this only
    # bounds how long a single gateway call waits before being abandoned. Default 10.
    push_gateway_timeout_s: float = Field(
        default=10.0,
        gt=0,
        description="HTTP timeout (seconds) for a push-gateway POST.",
    )

    # --- VoIP / TURN ----------------------------------------------------------
    # TURN relay servers advertised to clients via GET /_matrix/client/v3/voip/
    # turnServer, so Element/FluffyChat calls work across NATs. Credentials use
    # coturn's REST scheme (`use-auth-secret`): a time-limited username plus an
    # HMAC-SHA1 password derived from ``turn_shared_secret``. Leave ``turn_uris``
    # empty (the default) to advertise no TURN servers. The env value is JSON,
    # e.g. NEURON_SERVER_TURN_URIS='["turn:turn.example.org:3478?transport=udp"]'.
    turn_uris: list[str] = Field(
        default_factory=list,
        description="TURN server URIs advertised to clients (JSON list in the env).",
    )
    # Must match coturn's ``static-auth-secret``. If unset, /voip/turnServer
    # returns no servers even when turn_uris is set.
    turn_shared_secret: SecretStr | None = Field(
        default=None,
        description="Shared secret for coturn's REST credential scheme.",
    )
    # How long (seconds) issued TURN credentials stay valid. Default 24 hours.
    turn_ttl_s: int = Field(
        default=86400, gt=0, description="Lifetime (seconds) of issued TURN credentials."
    )

    # --- Observability ------------------------------------------------------
    # Expose a Prometheus /metrics endpoint (HTTP request counts + latency, process
    # metrics). Off by default; needs the `metrics` extra (prometheus-client), which
    # is imported lazily so the default/desktop build pulls in nothing. Restrict
    # access to /metrics at the proxy/network level.
    metrics_enabled: bool = Field(
        default=False, description="Expose a Prometheus /metrics endpoint."
    )

    # Where the ASGI server binds when run via `python -m neuron_server`.
    bind_host: str = Field(default="127.0.0.1", description="ASGI bind host.")
    bind_port: int = Field(default=8008, gt=0, description="ASGI bind port.")

    # --- Reverse proxy ------------------------------------------------------
    # When Neuron runs behind a reverse proxy / load balancer, the TCP peer is the
    # proxy, so the real client address arrives in X-Forwarded-For and the original
    # scheme in X-Forwarded-Proto. Honouring those headers blindly lets clients
    # spoof their IP, so we only trust them from the proxy IP(s) listed here.
    # Comma-separated IPs of your proxy hop(s), e.g. "10.0.0.1,10.0.0.2"; "*" trusts
    # any immediate peer (only safe when the server is reachable solely through the
    # proxy, e.g. bound to localhost). Empty (the default) trusts no proxy headers —
    # correct for a directly-exposed or desktop server.
    trusted_proxies: str = Field(
        default="",
        description="Comma-separated proxy IPs to trust for X-Forwarded-* (or '*').",
    )

    def trusted_proxy_set(self) -> frozenset[str]:
        """Parse ``trusted_proxies`` into a set of peer IPs (or ``{'*'}``)."""
        return frozenset(p.strip() for p in self.trusted_proxies.split(",") if p.strip())

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
    # Mark the session cookie Secure so browsers only send it over HTTPS. Off by
    # default for local/desktop use over plain HTTP; turn ON in any production
    # deployment served over HTTPS (the cookie carries the admin login session).
    session_https_only: bool = Field(
        default=False,
        description="Set the admin-console session cookie Secure (HTTPS-only).",
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
