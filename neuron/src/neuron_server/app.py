# SPDX-License-Identifier: Apache-2.0
"""The ``neuron_server`` ASGI application (HS-0 foundation).

Wires together:

- a **lifespan** that connects the database, runs migrations, and records/guards
  the server's identity;
- the spec-discovery endpoints (``GET /_matrix/client/versions`` and
  ``GET /.well-known/matrix/client``) and a ``/health`` probe;
- a catch-all that returns the spec's ``M_UNRECOGNIZED`` error for any other
  ``/_matrix`` request, so unknown endpoints fail in the documented way.

Run locally::

    NEURON_SERVER_NAME=neuron.local \\
    NEURON_SERVER_DATABASE_URL=sqlite:///./neuron_server.db \\
    python -m neuron_server
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from neuron_core import branding, configure_logging, get_logger
from neuron_server.admin.service import AdminService
from neuron_server.api import console as console_ui
from neuron_server.api.client_auth import router as client_auth_router
from neuron_server.api.client_keys import router as client_keys_router
from neuron_server.api.client_media import router as client_media_router
from neuron_server.api.client_misc import router as client_misc_router
from neuron_server.api.client_rooms import router as client_rooms_router
from neuron_server.api.client_sync import router as client_sync_router
from neuron_server.api.federation_backfill import router as federation_backfill_router
from neuron_server.api.federation_invite import router as federation_invite_router
from neuron_server.api.federation_join import router as federation_join_router
from neuron_server.api.federation_keys import router as federation_keys_router
from neuron_server.api.federation_leave import router as federation_leave_router
from neuron_server.api.federation_read import router as federation_read_router
from neuron_server.api.federation_transactions import router as federation_transactions_router
from neuron_server.api.synapse_admin import router as synapse_admin_router
from neuron_server.auth.service import AuthService
from neuron_server.config import NeuronServerSettings
from neuron_server.e2ee.service import E2EEService
from neuron_server.errors import MatrixError, unrecognized
from neuron_server.federation.client import FederationClient
from neuron_server.federation.flusher import RetryFlusher
from neuron_server.federation.membership import FederatedMembership
from neuron_server.federation.sender import FederationSender
from neuron_server.keys.resolver import ServerKeyResolver
from neuron_server.keys.service import ServerKeyService
from neuron_server.media.service import MediaService
from neuron_server.media.store import build_media_store
from neuron_server.metrics import install_metrics
from neuron_server.proxy import ProxyHeadersMiddleware, client_ip
from neuron_server.ratelimit import build_rate_limiters
from neuron_server.rooms.service import RoomService
from neuron_server.spec import SUPPORTED_SPEC_VERSIONS, UNSTABLE_FEATURES
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.metadata import get_metadata, set_metadata
from neuron_server.storage.migrations import run_migrations
from neuron_server.sync.notifier import build_notifier
from neuron_server.sync.service import SyncService
from neuron_server.typing_state import TypingHandler

log = get_logger(__name__)

_SERVER_NAME_KEY = "server_name"


async def _ensure_server_identity(db: Database, settings: NeuronServerSettings) -> None:
    """Record the server name on first init; refuse to start if it changed.

    A homeserver's server name is permanent — its Matrix IDs and (later) signing
    keys depend on it. Binding a database to a different name would corrupt those
    relationships, so we fail fast instead.
    """
    existing = await get_metadata(db, _SERVER_NAME_KEY)
    if existing is None:
        await set_metadata(db, _SERVER_NAME_KEY, settings.name)
        log.info("initialized server identity", extra={"server_name": settings.name})
    elif existing != settings.name:
        raise RuntimeError(
            f"database belongs to server_name={existing!r} but configured "
            f"NEURON_SERVER_NAME={settings.name!r}; refusing to start"
        )


def create_app(settings: NeuronServerSettings | None = None) -> FastAPI:
    """Build and configure the homeserver application."""
    settings = settings or NeuronServerSettings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = connect_database(
            settings.database_url,
            pool_size=settings.db_pool_size,
            instance_name=settings.instance_name,
        )
        await db.connect()
        # Serialize startup across workers (no-op on SQLite): concurrent migrations
        # / sequence-seeding against one shared database are not safe to run twice.
        async with db.startup_lock():
            newly = await run_migrations(db)
            await db.ensure_stream_sequences()
            await _ensure_server_identity(db, settings)
        log.info("database ready", extra={"newly_applied_migrations": newly})
        notifier = build_notifier(settings, db)
        app.state.db = db
        app.state.rate_limiters = build_rate_limiters(settings)
        app.state.notify = notifier.notify
        app.state.typing = TypingHandler(db, notify=notifier.notify)
        app.state.server_keys = await ServerKeyService.load_or_create(db, settings)
        app.state.federation_client = FederationClient(
            settings.name, app.state.server_keys.signing_key
        )
        app.state.server_key_resolver = ServerKeyResolver(
            db, settings.name, app.state.server_keys, app.state.federation_client
        )
        app.state.auth = AuthService(
            db,
            settings.name,
            settings.registration_enabled,
            first_user_admin=settings.first_user_admin,
            uia_session_ttl_s=settings.uia_session_ttl_s,
        )
        app.state.federation_sender = FederationSender(
            db, settings.name, app.state.federation_client
        )
        app.state.rooms = RoomService(
            db,
            settings.name,
            app.state.server_keys.signing_key,
            notify=notifier.notify,
            federation_sender=app.state.federation_sender.send_event,
            state_res_v2=settings.state_res_v2,
        )
        app.state.fed_membership = FederatedMembership(
            db,
            settings.name,
            app.state.server_keys.signing_key,
            app.state.federation_client,
            app.state.server_key_resolver,
            notify=notifier.notify,
            apply_event=app.state.rooms.apply_remote_event,
        )
        app.state.sync = SyncService(db, notifier, typing=app.state.typing)
        app.state.media = MediaService(
            build_media_store(settings),
            db,
            settings.name,
            settings.max_upload_bytes,
        )
        app.state.e2ee = E2EEService(db, notify=notifier.notify)
        app.state.admin = AdminService(
            db, settings.name, rooms=app.state.rooms, media=app.state.media
        )
        flusher = RetryFlusher(
            app.state.federation_sender.retry_all, settings.federation_retry_interval_s
        )
        app.state.retry_flusher = flusher
        # Multi-writer stream-position heartbeat (Postgres only): advances an idle
        # worker's stored positions to the committed max so it stops holding the
        # shared /sync floor back. No-op/pointless for SQLite's single instance.
        position_heartbeat: RetryFlusher | None = None
        if settings.database_url.startswith(("postgresql://", "postgres://")):
            position_heartbeat = RetryFlusher(
                db.heartbeat_positions,
                settings.position_heartbeat_interval_s,
                name="position heartbeat",
            )
        # Periodically drop expired UIA sessions so the (now persisted) table can't
        # grow without bound from abandoned registrations. Runs on every backend;
        # cadence is the TTL capped to 10 min so rows clear soon after expiry.
        uia_sweeper = RetryFlusher(
            app.state.auth.sweep_uia,
            max(60.0, min(settings.uia_session_ttl_s, 600.0)),
            name="uia session sweep",
        )
        # Start the notifier transport (a no-op for the in-process backend) before
        # serving, so cross-worker /sync wakes are received from the first request.
        await notifier.start()
        flusher.start()
        uia_sweeper.start()
        if position_heartbeat is not None:
            position_heartbeat.start()
        try:
            yield
        finally:
            await flusher.stop()
            await uia_sweeper.stop()
            if position_heartbeat is not None:
                await position_heartbeat.stop()
            # Tear down the notifier (closes the dedicated LISTEN connection)
            # before the pool, so it never outlives the database.
            await notifier.stop()
            await db.disconnect()

    app = FastAPI(title="Neuron Server", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings

    # Signed session cookie for the built-in admin console (login state + CSRF).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.effective_session_secret(),
        session_cookie=settings.session_cookie_name,
        same_site="lax",
        # HTTP for local/desktop; set NEURON_SERVER_SESSION_HTTPS_ONLY=true in any
        # production deployment served over HTTPS (the cookie carries the login).
        https_only=settings.session_https_only,
    )

    # Trusted reverse-proxy support: rewrite the client IP/scheme from X-Forwarded-*
    # when behind a proxy. Added last so it runs OUTERMOST — every later middleware
    # and route then sees the real client address. Skipped (raw TCP peer used) when
    # no proxies are trusted, the correct default for a direct/desktop server.
    trusted = settings.trusted_proxy_set()
    if trusted:
        app.add_middleware(ProxyHeadersMiddleware, trusted=trusted)

    @app.exception_handler(MatrixError)
    async def _on_matrix_error(request: Request, exc: MatrixError) -> JSONResponse:
        return exc.to_response()

    @app.get("/_matrix/client/versions")
    async def versions() -> dict[str, Any]:
        return {
            "versions": list(SUPPORTED_SPEC_VERSIONS),
            "unstable_features": dict(UNSTABLE_FEATURES),
        }

    @app.get("/.well-known/matrix/client")
    async def well_known_client() -> dict[str, Any]:
        return {"m.homeserver": {"base_url": settings.public_base_url}}

    @app.get("/health")
    async def health() -> PlainTextResponse:
        return PlainTextResponse("OK")

    @app.get("/", include_in_schema=False)
    async def landing() -> HTMLResponse:
        return HTMLResponse(branding.landing_page_html(settings.name))

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> Response:
        return Response(branding.mark_svg(branding.NAVY), media_type="image/svg+xml")

    async def _can_register(auth: AuthService, admin: AdminService, token: str | None) -> bool:
        """Onboarding is open when registration is on, or a valid invite token is held."""
        if auth.registration_enabled:
            return True
        return bool(token) and await admin.registration_token_valid(token or "")

    @app.get("/get-started", include_in_schema=False)
    async def get_started(request: Request) -> HTMLResponse:
        auth: AuthService = app.state.auth
        admin: AdminService = app.state.admin
        token = request.query_params.get("token") or None
        can = await _can_register(auth, admin, token)
        return HTMLResponse(
            branding.get_started_html(settings.name, can_register=can, token=token)
        )

    @app.post("/get-started", include_in_schema=False)
    async def get_started_submit(request: Request) -> HTMLResponse:
        auth: AuthService = app.state.auth
        admin: AdminService = app.state.admin
        form = await request.form()
        token = str(form.get("token") or "").strip() or None
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")

        # Re-check the gate at submit time (an open server, or a token still valid).
        if not await _can_register(auth, admin, token):
            return HTMLResponse(
                branding.get_started_html(settings.name, can_register=False),
                status_code=403,
            )

        # Same sign-up throttle as the Matrix /register endpoint (per client IP).
        app.state.rate_limiters.check_registration(client_ip(request))

        try:
            result = await auth.register(
                localpart=username or None,
                password=password or None,
                device_id=None,
                initial_device_display_name=None,
                inhibit_login=True,
            )
        except MatrixError as exc:
            # The account was not created, so the invite token is untouched — the
            # user can fix the error (e.g. a taken username) and submit again.
            return HTMLResponse(
                branding.get_started_html(
                    settings.name,
                    can_register=True,
                    token=token,
                    error=exc.error,
                    username=username,
                ),
                status_code=exc.status_code,
            )

        # Only now claim a use of the invite (when the server is closed, the token is
        # what authorised this account). On an open server the token is just a link.
        if token and not auth.registration_enabled:
            await admin.consume_registration_token(token)

        user_id = result["user_id"] if isinstance(result, dict) else result.user_id
        return HTMLResponse(branding.welcome_html(settings.name, user_id))

    # Client-Server API routers (registered before the catch-all so their
    # specific routes match first).
    app.include_router(client_auth_router)
    app.include_router(client_rooms_router)
    app.include_router(client_sync_router)
    app.include_router(client_media_router)
    app.include_router(client_keys_router)
    app.include_router(client_misc_router)
    app.include_router(synapse_admin_router)
    app.include_router(federation_keys_router)
    app.include_router(federation_read_router)
    app.include_router(federation_transactions_router)
    app.include_router(federation_join_router)
    app.include_router(federation_leave_router)
    app.include_router(federation_invite_router)
    app.include_router(federation_backfill_router)

    # The built-in admin console (web UI under /console/*) + its session-auth
    # exception handlers. Backed by the in-process AdminService/AuthService above.
    console_ui.install(app)

    # Anything else under /_matrix is an unknown endpoint: the spec says reply
    # 404 with M_UNRECOGNIZED. Registered last so specific routes match first.
    @app.api_route(
        "/_matrix/{_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    )
    async def matrix_unrecognized(_path: str) -> JSONResponse:
        raise unrecognized()

    # Optional Prometheus /metrics endpoint + request-metrics middleware (no-op
    # unless enabled; prometheus_client is imported lazily).
    install_metrics(app, settings)

    return app
