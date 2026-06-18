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
from starlette.responses import JSONResponse, PlainTextResponse

from neuron_core import configure_logging, get_logger
from neuron_server.admin.service import AdminService
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
from neuron_server.federation.membership import FederatedMembership
from neuron_server.federation.sender import FederationSender
from neuron_server.keys.resolver import ServerKeyResolver
from neuron_server.keys.service import ServerKeyService
from neuron_server.media.service import MediaService
from neuron_server.media.store import FilesystemMediaStore
from neuron_server.rooms.service import RoomService
from neuron_server.spec import SUPPORTED_SPEC_VERSIONS, UNSTABLE_FEATURES
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.metadata import get_metadata, set_metadata
from neuron_server.storage.migrations import run_migrations
from neuron_server.sync.notifier import StreamNotifier
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
        db = connect_database(settings.database_url)
        await db.connect()
        newly = await run_migrations(db)
        log.info("database ready", extra={"newly_applied_migrations": newly})
        await _ensure_server_identity(db, settings)
        notifier = StreamNotifier()
        app.state.db = db
        app.state.notify = notifier.notify
        app.state.typing = TypingHandler(notify=notifier.notify)
        app.state.server_keys = await ServerKeyService.load_or_create(db, settings)
        app.state.federation_client = FederationClient(
            settings.name, app.state.server_keys.signing_key
        )
        app.state.server_key_resolver = ServerKeyResolver(
            db, settings.name, app.state.server_keys, app.state.federation_client
        )
        app.state.auth = AuthService(db, settings.name, settings.registration_enabled)
        app.state.federation_sender = FederationSender(
            db, settings.name, app.state.federation_client
        )
        app.state.rooms = RoomService(
            db,
            settings.name,
            app.state.server_keys.signing_key,
            notify=notifier.notify,
            federation_sender=app.state.federation_sender.send_event,
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
            FilesystemMediaStore(settings.media_store_path),
            db,
            settings.name,
            settings.max_upload_bytes,
        )
        app.state.e2ee = E2EEService(db, notify=notifier.notify)
        app.state.admin = AdminService(db, settings.name)
        try:
            yield
        finally:
            await db.disconnect()

    app = FastAPI(title="Neuron Server", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings

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

    # Anything else under /_matrix is an unknown endpoint: the spec says reply
    # 404 with M_UNRECOGNIZED. Registered last so specific routes match first.
    @app.api_route(
        "/_matrix/{_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    )
    async def matrix_unrecognized(_path: str) -> JSONResponse:
        raise unrecognized()

    return app
