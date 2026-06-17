"""The neuron-console FastAPI application.

Phase 1 added read-only browsing. Phase 2 adds **write actions** (create/modify/
deactivate users, reset passwords, shadow-ban, registration tokens, server
notices, room block/delete, redaction) with **CSRF protection** and **MAS-aware
guards** (actions Synapse disables under delegated auth are blocked with a clear
message).

Run locally::

    NEURON_SYNAPSE_BASE_URL=http://localhost:8008 \\
    NEURON_SYNAPSE_ADMIN_TOKEN=<server-admin token> \\
    NEURON_SYNAPSE_SERVER_NAME=neuron.local \\
    NEURON_CONSOLE_PASSWORD=letmein \\
    uvicorn neuron_console.app:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, Response

from neuron_console.config import ConsoleSettings
from neuron_console.deps import (
    CsrfError,
    MasDisabledError,
    NotAuthenticated,
    csrf_protect,
    ensure_classic_auth,
    get_admin,
    get_settings,
    get_supervisor,
    require_login,
)
from neuron_console.security import get_csrf_token
from neuron_core import MatrixClient, SynapseAdminClient, configure_logging, get_logger
from neuron_core.errors import SynapseAdminError
from neuron_supervisor import Supervisor
from neuron_supervisor.core import SupervisorError

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
log = get_logger(__name__)


def create_app(settings: ConsoleSettings | None = None) -> FastAPI:
    """Build and configure the console application."""
    settings = settings or ConsoleSettings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        admin = SynapseAdminClient(
            settings.synapse_base_url,
            settings.synapse_admin_token.get_secret_value(),
            timeout=settings.http_timeout_seconds,
        )
        # The supervision bot's CS-API client is optional: promotion works with
        # just the admin token; kick/ban additionally need the bot token.
        bot: MatrixClient | None = None
        if settings.has_supervisor_bot():
            bot = MatrixClient(
                settings.synapse_base_url,
                settings.supervisor_bot_token.get_secret_value(),
                timeout=settings.http_timeout_seconds,
            )
        app.state.admin = admin
        app.state.supervisor = Supervisor(admin, settings.supervisor_bot_user_id, bot=bot)
        try:
            yield
        finally:
            await admin.aclose()
            if bot is not None:
                await bot.aclose()

    app = FastAPI(title="Neuron Console", lifespan=lifespan)
    app.state.settings = settings

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.effective_session_secret(),
        session_cookie=settings.session_cookie_name,
        same_site="lax",
        https_only=False,  # dev over HTTP; set True behind HTTPS in production
    )
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    _register_exception_handlers(app)
    _register_routes(app)
    return app


# ---------------------------------------------------------------------------
# Rendering helper: injects CSRF token, MAS flag, server name, and any one-shot
# "flash" message into every template context so handlers stay short.
# ---------------------------------------------------------------------------
def _render(
    request: Request, name: str, *, status_code: int = 200, **context: Any
) -> Response:
    settings: ConsoleSettings = request.app.state.settings
    base: dict[str, Any] = {
        "csrf_token": get_csrf_token(request),
        "mas_enabled": settings.mas_enabled(),
        "server_name": settings.synapse_server_name,
        "flash": request.session.pop("flash", None),
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def _flash(request: Request, message: str) -> None:
    """Store a one-shot message shown on the next rendered page."""
    request.session["flash"] = message


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotAuthenticated)
    async def _on_not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(CsrfError)
    async def _on_csrf(request: Request, exc: CsrfError) -> Response:
        return _render(
            request,
            "error.html",
            status_code=400,
            status=400,
            errcode="CSRF",
            message="Your session expired or the form token was invalid. Please try again.",
        )

    @app.exception_handler(MasDisabledError)
    async def _on_mas_disabled(request: Request, exc: MasDisabledError) -> Response:
        return _render(
            request,
            "error.html",
            status_code=409,
            status=409,
            errcode="MAS_DISABLED",
            message=(
                f"{exc.action} is handled by the Matrix Authentication Service in this "
                "deployment, so it is not available from this console."
            ),
        )

    @app.exception_handler(SupervisorError)
    async def _on_supervisor_error(request: Request, exc: SupervisorError) -> Response:
        return _render(
            request,
            "error.html",
            status_code=409,
            status=409,
            errcode="SUPERVISION",
            message=str(exc),
        )

    @app.exception_handler(SynapseAdminError)
    async def _on_admin_error(request: Request, exc: SynapseAdminError) -> Response:
        log.warning(
            "synapse admin error",
            extra={"status": exc.status_code, "errcode": exc.errcode, "path": request.url.path},
        )
        return _render(
            request,
            "error.html",
            status_code=502,
            status=exc.status_code,
            errcode=exc.errcode,
            message=exc.message,
        )


def _register_routes(app: FastAPI) -> None:
    # --- health & auth ------------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login")
    async def login_form(request: Request) -> Response:
        return _render(request, "login.html", error=None)

    @app.post("/login")
    async def login_submit(
        request: Request,
        password: str = Form(...),
        settings: ConsoleSettings = Depends(get_settings),
    ) -> Response:
        if settings.check_password(password):
            request.session["authenticated"] = True
            return RedirectResponse("/", status_code=303)
        return _render(request, "login.html", status_code=401, error="Incorrect password.")

    @app.get("/logout")
    async def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- dashboard ----------------------------------------------------------
    @app.get("/")
    async def dashboard(
        request: Request,
        _: None = Depends(require_login),
        admin: SynapseAdminClient = Depends(get_admin),
    ) -> Response:
        version = await admin.get_server_version()
        users = await admin.list_users(limit=1)
        rooms = await admin.list_rooms(limit=1)
        return _render(
            request,
            "dashboard.html",
            version=version,
            user_count=users.total,
            room_count=rooms.total_rooms,
        )

    # --- users: read --------------------------------------------------------
    @app.get("/users")
    async def users_list(
        request: Request,
        _: None = Depends(require_login),
        admin: SynapseAdminClient = Depends(get_admin),
        search: str | None = None,
        from_token: str | None = None,
        limit: int = 50,
    ) -> Response:
        page = await admin.list_users(from_token=from_token, limit=limit, name=search or None)
        return _render(
            request, "users.html", page=page, search=search or "", limit=limit,
            next_token=page.next_token,
        )

    @app.get("/users/new")
    async def user_new_form(
        request: Request, _: None = Depends(require_login)
    ) -> Response:
        return _render(request, "user_new.html")

    @app.post("/users/new")
    async def user_new_submit(
        request: Request,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
        settings: ConsoleSettings = Depends(get_settings),
        localpart: str = Form(...),
        password: str = Form(...),
        displayname: str = Form(""),
        make_admin: bool = Form(False),
    ) -> Response:
        try:
            user_id = settings.build_user_id(localpart.strip())
        except ValueError as exc:
            return _render(request, "user_new.html", status_code=400, error=str(exc))
        # Setting the admin flag is disabled under MAS; only honour it in classic mode.
        admin_flag = (make_admin and not settings.mas_enabled()) or None
        _user, created = await admin.upsert_user(
            user_id, password=password, displayname=displayname or None, admin=admin_flag
        )
        _flash(request, f"{'Created' if created else 'Updated'} {user_id}.")
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.get("/users/{user_id}")
    async def user_detail(
        request: Request,
        user_id: str,
        _: None = Depends(require_login),
        admin: SynapseAdminClient = Depends(get_admin),
    ) -> Response:
        user = await admin.get_user(user_id)
        return _render(request, "user_detail.html", user_id=user_id, user=user)

    # --- users: write -------------------------------------------------------
    @app.post("/users/{user_id}/modify")
    async def user_modify(
        request: Request,
        user_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
        settings: ConsoleSettings = Depends(get_settings),
        displayname: str = Form(""),
        make_admin: bool = Form(False),
    ) -> Response:
        # Admin-flag changes are disabled under MAS, so only send them in classic mode.
        admin_flag = None if settings.mas_enabled() else make_admin
        await admin.upsert_user(user_id, displayname=displayname, admin=admin_flag)
        _flash(request, "Profile updated.")
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.post("/users/{user_id}/reset-password")
    async def user_reset_password(
        request: Request,
        user_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
        settings: ConsoleSettings = Depends(get_settings),
        new_password: str = Form(...),
        logout_devices: bool = Form(False),
    ) -> Response:
        ensure_classic_auth(settings, "Password reset")
        await admin.reset_password(user_id, new_password, logout_devices=logout_devices)
        _flash(request, "Password reset.")
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.post("/users/{user_id}/shadow-ban")
    async def user_shadow_ban(
        request: Request,
        user_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
        banned: bool = Form(...),
    ) -> Response:
        await admin.set_shadow_ban(user_id, banned)
        _flash(request, "Shadow-ban applied." if banned else "Shadow-ban removed.")
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.get("/users/{user_id}/deactivate")
    async def user_deactivate_confirm(
        request: Request,
        user_id: str,
        _: None = Depends(require_login),
    ) -> Response:
        return _render(request, "user_deactivate.html", user_id=user_id)

    @app.post("/users/{user_id}/deactivate")
    async def user_deactivate_submit(
        request: Request,
        user_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
        erase: bool = Form(False),
    ) -> Response:
        await admin.deactivate_user(user_id, erase=erase)
        _flash(request, f"Deactivated {user_id}{' (erased)' if erase else ''}.")
        return RedirectResponse(f"/users/{user_id}", status_code=303)

    @app.post("/users/{user_id}/redact")
    async def user_redact(
        request: Request,
        user_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
    ) -> Response:
        redact_id = await admin.redact_user_events(user_id, rooms=[])
        return RedirectResponse(f"/redactions/{redact_id}", status_code=303)

    @app.get("/redactions/{redact_id}")
    async def redact_status(
        request: Request,
        redact_id: str,
        _: None = Depends(require_login),
        admin: SynapseAdminClient = Depends(get_admin),
    ) -> Response:
        status = await admin.get_redact_status(redact_id)
        return _render(request, "redact_status.html", redact_id=redact_id, status=status)

    # --- rooms: read --------------------------------------------------------
    @app.get("/rooms")
    async def rooms_list(
        request: Request,
        _: None = Depends(require_login),
        admin: SynapseAdminClient = Depends(get_admin),
        search: str | None = None,
        from_offset: int = 0,
        limit: int = 50,
    ) -> Response:
        page = await admin.list_rooms(
            from_offset=from_offset, limit=limit, search_term=search or None
        )
        return _render(request, "rooms.html", page=page, search=search or "", limit=limit)

    @app.get("/rooms/{room_id}")
    async def room_detail(
        request: Request,
        room_id: str,
        _: None = Depends(require_login),
        admin: SynapseAdminClient = Depends(get_admin),
        settings: ConsoleSettings = Depends(get_settings),
    ) -> Response:
        room = await admin.get_room(room_id)
        members = await admin.get_room_members(room_id)
        state = await admin.get_room_state(room_id)
        return _render(
            request, "room_detail.html", room_id=room_id, room=room, members=members,
            state=state, bot_user_id=settings.supervisor_bot_user_id,
            bot_token_configured=settings.has_supervisor_bot(),
        )

    # --- rooms: write -------------------------------------------------------
    @app.post("/rooms/{room_id}/block")
    async def room_block(
        request: Request,
        room_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
        block: bool = Form(...),
    ) -> Response:
        await admin.set_room_block(room_id, block)
        _flash(request, "Room blocked." if block else "Room unblocked.")
        return RedirectResponse(f"/rooms/{room_id}", status_code=303)

    @app.get("/rooms/{room_id}/delete")
    async def room_delete_confirm(
        request: Request,
        room_id: str,
        _: None = Depends(require_login),
    ) -> Response:
        return _render(request, "room_delete.html", room_id=room_id)

    @app.post("/rooms/{room_id}/delete")
    async def room_delete_submit(
        request: Request,
        room_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
        block: bool = Form(False),
        purge: bool = Form(True),
    ) -> Response:
        delete_id = await admin.delete_room(room_id, block=block, purge=purge)
        return RedirectResponse(f"/room-deletions/{delete_id}", status_code=303)

    @app.get("/room-deletions/{delete_id}")
    async def room_delete_status(
        request: Request,
        delete_id: str,
        _: None = Depends(require_login),
        admin: SynapseAdminClient = Depends(get_admin),
    ) -> Response:
        status = await admin.get_room_delete_status(delete_id)
        return _render(request, "room_delete_status.html", delete_id=delete_id, status=status)

    # --- supervision --------------------------------------------------------
    @app.get("/supervision")
    async def supervision_page(
        request: Request,
        _: None = Depends(require_login),
        settings: ConsoleSettings = Depends(get_settings),
    ) -> Response:
        return _render(
            request,
            "supervision.html",
            bot_user_id=settings.supervisor_bot_user_id,
            bot_token_configured=settings.has_supervisor_bot(),
        )

    @app.post("/supervision/promote-all")
    async def supervision_promote_all(
        request: Request,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        supervisor: Supervisor = Depends(get_supervisor),
    ) -> Response:
        results = await supervisor.ensure_admin_in_all_rooms()
        promoted = sum(1 for r in results if r["promoted"])
        _flash(request, f"Promoted the bot in {promoted} of {len(results)} room(s).")
        return RedirectResponse("/supervision", status_code=303)

    @app.post("/rooms/{room_id}/promote-bot")
    async def room_promote_bot(
        request: Request,
        room_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        supervisor: Supervisor = Depends(get_supervisor),
    ) -> Response:
        await supervisor.ensure_admin(room_id)
        _flash(request, "Promoted the supervision bot to admin in this room.")
        return RedirectResponse(f"/rooms/{room_id}", status_code=303)

    @app.post("/rooms/{room_id}/kick")
    async def room_kick(
        request: Request,
        room_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        supervisor: Supervisor = Depends(get_supervisor),
        user_id: str = Form(...),
        reason: str = Form(""),
    ) -> Response:
        await supervisor.kick(room_id, user_id, reason=reason or None)
        _flash(request, f"Kicked {user_id}.")
        return RedirectResponse(f"/rooms/{room_id}", status_code=303)

    @app.post("/rooms/{room_id}/ban")
    async def room_ban(
        request: Request,
        room_id: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        supervisor: Supervisor = Depends(get_supervisor),
        user_id: str = Form(...),
        reason: str = Form(""),
    ) -> Response:
        await supervisor.ban(room_id, user_id, reason=reason or None)
        _flash(request, f"Banned {user_id}.")
        return RedirectResponse(f"/rooms/{room_id}", status_code=303)

    # --- registration tokens ------------------------------------------------
    @app.get("/registration-tokens")
    async def tokens_list(
        request: Request,
        _: None = Depends(require_login),
        admin: SynapseAdminClient = Depends(get_admin),
    ) -> Response:
        tokens = await admin.list_registration_tokens()
        return _render(request, "registration_tokens.html", tokens=tokens)

    @app.post("/registration-tokens/new")
    async def tokens_new(
        request: Request,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
        uses_allowed: str = Form(""),
    ) -> Response:
        uses = int(uses_allowed) if uses_allowed.strip().isdigit() else None
        result = await admin.create_registration_token(uses_allowed=uses)
        _flash(request, f"Created registration token: {result.get('token', '(unknown)')}")
        return RedirectResponse("/registration-tokens", status_code=303)

    @app.post("/registration-tokens/{token}/delete")
    async def tokens_delete(
        request: Request,
        token: str,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
    ) -> Response:
        await admin.delete_registration_token(token)
        _flash(request, "Registration token deleted.")
        return RedirectResponse("/registration-tokens", status_code=303)

    # --- server notice ------------------------------------------------------
    @app.get("/server-notice")
    async def server_notice_form(
        request: Request, _: None = Depends(require_login)
    ) -> Response:
        return _render(request, "server_notice.html")

    @app.post("/server-notice")
    async def server_notice_submit(
        request: Request,
        _: None = Depends(require_login),
        __: None = Depends(csrf_protect),
        admin: SynapseAdminClient = Depends(get_admin),
        user_id: str = Form(...),
        message: str = Form(...),
    ) -> Response:
        await admin.send_server_notice(user_id.strip(), message)
        _flash(request, f"Server notice sent to {user_id.strip()}.")
        return RedirectResponse("/server-notice", status_code=303)

    # --- reports ------------------------------------------------------------
    @app.get("/reports")
    async def reports_list(
        request: Request,
        _: None = Depends(require_login),
        admin: SynapseAdminClient = Depends(get_admin),
        from_offset: int = 0,
        limit: int = 50,
    ) -> Response:
        page = await admin.list_event_reports(from_offset=from_offset, limit=limit)
        return _render(request, "reports.html", page=page)


# The ASGI entrypoint uvicorn imports (e.g. `uvicorn neuron_console.app:app`).
app = create_app()
