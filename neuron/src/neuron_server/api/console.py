# SPDX-License-Identifier: Apache-2.0
"""The built-in admin console (merged into the homeserver).

This is the web UI an operator uses to manage their server — sign-in, an overview,
user management, room inspection and invite tokens — served by the SAME app as the
Matrix Client-Server API. It authenticates the operator's own **admin account**
(Matrix username + password) using a signed session cookie, and drives the server's
**in-process** services (``app.state.admin`` / ``app.state.auth``) directly, so it
needs no admin token and no second process.

Pages are rendered as branded pure-Python HTML via :mod:`neuron_core.branding`
(no Jinja templates / static files), which keeps the frozen desktop bundle simple.

Operations that the homeserver does not yet fully implement (shadow-ban, server
notices, room block/delete, redaction, content reports) are shown as disabled
"coming soon" controls rather than wired to stubs.
"""

from __future__ import annotations

import html
import io
import json
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from neuron_core import branding
from neuron_server.admin.service import AdminService
from neuron_server.auth.passwords import verify_password
from neuron_server.config import NeuronServerSettings
from neuron_server.security import get_csrf_token, verify_csrf
from neuron_server.storage import accounts, metadata
from neuron_server.storage import admin as admin_store

router = APIRouter()

_PAGE_SIZE = 25
_e = html.escape


# --- exceptions -------------------------------------------------------------
class NotAuthenticated(Exception):
    """No valid console session — redirect to the login page."""


class CsrfError(Exception):
    """A state-changing request had a missing/invalid CSRF token."""


# --- small accessors --------------------------------------------------------
def _settings(request: Request) -> NeuronServerSettings:
    return request.app.state.settings


def _admin(request: Request) -> AdminService:
    return request.app.state.admin


def require_console_admin(request: Request) -> str:
    """Gate a console page: return the signed-in admin's user id, else redirect."""
    user = request.session.get("console_user")
    if not user:
        raise NotAuthenticated()
    return str(user)


async def csrf_protect(request: Request, csrf_token: str = Form("")) -> None:
    """Reject a state-changing request whose CSRF token doesn't match the session."""
    if not verify_csrf(request, csrf_token):
        raise CsrfError()


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _full_user_id(settings: NeuronServerSettings, entered: str) -> str:
    entered = entered.strip()
    if entered.startswith("@"):
        return entered
    return f"@{entered}:{settings.name}"


# --- rendering helpers ------------------------------------------------------
def _page(
    request: Request, title: str, active: str, body: str, *, status: int = 200
) -> HTMLResponse:
    settings = _settings(request)
    flash = request.session.pop("flash", None)
    doc = branding.admin_shell(
        title, body, active=active, server_name=settings.name, flash=flash
    )
    return HTMLResponse(doc, status_code=status)


def _csrf_field(request: Request) -> str:
    return f'<input type="hidden" name="csrf_token" value="{_e(get_csrf_token(request))}">'


def _pill(on: bool, on_label: str, off_label: str) -> str:
    cls = "on" if on else "off"
    return f'<span class="pill {cls}">{_e(on_label if on else off_label)}</span>'


def _fmt_ts(ms: int) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


# The onchange handler on each bulk-select row checkbox (recomputes the selection
# count and shows/hides the bulk action bar).
_BULK_ONCHANGE = " onchange=\"neuronBulk(this.closest('.bulk-form'))\""


# --- auth -------------------------------------------------------------------
@router.get("/console/login", include_in_schema=False)
async def login_form(request: Request) -> Response:
    if request.session.get("console_user"):
        return RedirectResponse("/console", status_code=303)
    # A brand-new server has no account to sign in with yet — send the operator to
    # create the first one (which becomes the admin) instead of a dead-end login.
    if not await accounts.any_users(request.app.state.db):
        return RedirectResponse("/get-started", status_code=303)
    csrf = get_csrf_token(request)  # seed the session cookie + token
    has_passkeys = bool(await admin_store.all_passkey_ids(request.app.state.db))
    return HTMLResponse(
        branding.login_card_html(
            _settings(request).name,
            csrf_token=csrf,
            passkey_button=has_passkeys,
            script=_passkey_script(request) if has_passkeys else "",
        )
    )


@router.post("/console/login", include_in_schema=False)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
    next: str = Form("/console"),
) -> Response:
    settings = _settings(request)
    name = settings.name

    def _fail(message: str, status: int) -> Response:
        return HTMLResponse(
            branding.login_card_html(
                name, csrf_token=get_csrf_token(request), error=message, username=username
            ),
            status_code=status,
        )

    if not verify_csrf(request, csrf_token):
        return _fail("Your session expired — please try again.", 400)

    user_id = _full_user_id(settings, username)
    row = await accounts.get_user(request.app.state.db, user_id)
    if row is None or row.password_hash is None or not verify_password(password, row.password_hash):
        return _fail("Incorrect username or password.", 401)
    if row.deactivated:
        return _fail("That account has been deactivated.", 403)
    is_admin = user_id in settings.admin_user_ids() or row.admin
    if not is_admin:
        return _fail("That account is not a server administrator.", 403)

    request.session["console_user"] = user_id
    target = next if next.startswith("/console") else "/console"
    return RedirectResponse(target, status_code=303)


@router.get("/console/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/console/login", status_code=303)


# --- overview ---------------------------------------------------------------
@router.get("/console", include_in_schema=False)
async def overview(request: Request, _: str = Depends(require_console_admin)) -> Response:
    admin = _admin(request)
    version = admin.server_version()
    users = await admin.list_users(offset=0, limit=1, name=None, deactivated=None)
    rooms = await admin.list_rooms(offset=0, limit=1)
    server_ver = _e(version.get("server_version", "").replace("Neuron ", "")) or "—"

    def _stat(label: str, value: str) -> str:
        # Label above the value (the description sits on top of each card).
        return (
            f'<div class="stat"><div class="lbl">{label}</div>'
            f'<div class="num">{value}</div></div>'
        )

    stats = (
        '<div class="stat-grid">'
        + _stat("Users", str(users.get("total", 0)))
        + _stat("Rooms", str(rooms.get("total_rooms", 0)))
        + _stat("Server", server_ver)
        + _stat("Status", '<span class="pill on">running</span>')
        + "</div>"
    )
    quick = (
        '<div class="panel"><h2>Manage</h2><div class="row">'
        '<a class="btn" href="/console/users">Users</a>'
        '<a class="btn ghost" href="/console/rooms">Rooms</a>'
        '<a class="btn ghost" href="/console/invites">Invites</a>'
        '<a class="btn ghost" href="/get-started">Create account</a></div></div>'
    )
    moderation = (
        '<div class="panel"><h2>Moderation</h2>'
        '<p class="muted">Block or delete rooms, shadow-ban or redact a user, and review '
        "reported events from the Users, Rooms and Reports tabs.</p></div>"
    )
    body = f'<h1 class="page">Overview</h1>{stats}{quick}{moderation}'
    return _page(request, "Overview", "/console", body)


# --- users ------------------------------------------------------------------
@router.get("/console/users", include_in_schema=False)
async def users_list(
    request: Request,
    _: str = Depends(require_console_admin),
    q: str = "",
    status: str = "",
    offset: int = 0,
) -> Response:
    admin = _admin(request)
    offset = max(0, offset)
    # "" = all, "active" = not deactivated, "deactivated" = deactivated only.
    deactivated = {"active": False, "deactivated": True}.get(status)
    page = await admin.list_users(
        offset=offset, limit=_PAGE_SIZE, name=q or None, deactivated=deactivated
    )
    rows = ""
    for u in page.get("users", []):
        uid = str(u.get("name", ""))
        link = f'/console/users/{_quote(uid)}'
        display = _e(u.get("displayname") or "")
        created = _fmt_ts(int(u.get("creation_ts") or 0)) if u.get("creation_ts") else "—"
        rows += (
            f'<tr><td class="check"><input type="checkbox" class="rowcheck"'
            f' name="user_ids" value="{_e(uid)}"{_BULK_ONCHANGE}></td>'
            f'<td><a href="{link}">{_e(uid)}</a></td>'
            f"<td>{display}</td>"
            f'<td>{_pill(bool(u.get("admin")), "admin", "user")}</td>'
            f'<td>{_pill(not u.get("deactivated"), "active", "deactivated")}</td>'
            f'<td class="muted">{created}</td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="6" class="muted">No users found.</td></tr>'
    total = page.get("total", 0)

    def _opt(value: str, label: str) -> str:
        sel = " selected" if status == value else ""
        return f'<option value="{value}"{sel}>{label}</option>'

    search = (
        '<form class="row searchbar" method="get" action="/console/users">'
        f'<input class="q" name="q" value="{_e(q)}" placeholder="Search username">'
        f'<select name="status">{_opt("", "All")}{_opt("active", "Active")}'
        f'{_opt("deactivated", "Deactivated")}</select>'
        '<button class="btn sm" type="submit">Search</button>'
        '<a class="btn sm ghost" href="/console/users/new">New user</a></form>'
    )
    nav = _pager(
        "/console/users", offset, _PAGE_SIZE, total, q,
        extra=f"&status={status}" if status else "",
    )
    body = (
        f'<div class="spread"><h1 class="page">Users <span class="muted">({total})</span></h1>'
        f"{search}</div>"
        '<form class="bulk-form" method="post" action="/console/users/bulk">'
        f"{_csrf_field(request)}"
        '<div class="bulkbar"><strong><span data-count>0</span> selected</strong>'
        '<button class="btn sm" name="action" value="shadow_ban">Shadow-ban</button>'
        '<button class="btn sm ghost" name="action" value="unshadow_ban">Un-shadow-ban</button>'
        '<button class="btn sm danger" name="action" value="deactivate">Deactivate</button></div>'
        '<div class="panel"><table class="tbl"><thead><tr>'
        '<th class="check"><input type="checkbox" class="checkall"'
        ' onchange="neuronCheckAll(this)"></th>'
        '<th>User</th><th>Display name</th><th>Role</th><th>Status</th>'
        '<th>Created</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>{nav}</div></form>"
    )
    return _page(request, "Users", "/console/users", body)


@router.post("/console/users/bulk", include_in_schema=False)
async def users_bulk(
    request: Request,
    me: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    action: str = Form(""),
    user_ids: list[str] = Form([]),
) -> Response:
    admin = _admin(request)
    done = 0
    for uid in user_ids:
        if uid == me and action in ("deactivate", "shadow_ban"):
            continue  # never let an admin lock themselves out in bulk
        try:
            if action == "deactivate":
                await admin.deactivate_user(uid)
            elif action == "shadow_ban":
                await admin.set_shadow_ban(uid, True)
            elif action == "unshadow_ban":
                await admin.set_shadow_ban(uid, False)
            else:
                break
            done += 1
        except Exception:  # noqa: BLE001 - best-effort bulk; skip per-item failures
            continue
    labels = {
        "deactivate": "Deactivated",
        "shadow_ban": "Shadow-banned",
        "unshadow_ban": "Un-shadow-banned",
    }
    _flash(request, f"{labels.get(action, 'Updated')} {done} user(s).")
    return RedirectResponse("/console/users", status_code=303)


@router.get("/console/users/new", include_in_schema=False)
async def user_new_form(
    request: Request, _: str = Depends(require_console_admin), error: str = ""
) -> Response:
    settings = _settings(request)
    err = f'<div class="error">{_e(error)}</div>' if error else ""
    body = (
        '<h1 class="page">New user</h1>'
        f'<div class="panel" style="max-width:460px">{err}'
        '<form method="post" action="/console/users/new">'
        f"{_csrf_field(request)}"
        '<label for="lp">Username</label>'
        '<input id="lp" name="localpart" placeholder="alice" autocapitalize="none" required>'
        '<label for="dn">Display name (optional)</label>'
        '<input id="dn" name="displayname" placeholder="Alice">'
        '<label for="pw">Password</label>'
        '<input id="pw" name="password" type="password" required>'
        '<label class="row" style="font-weight:400;margin:.2rem 0 1rem">'
        '<input type="checkbox" name="make_admin" value="true" '
        'style="width:auto;margin:0 .5rem 0 0"> Make this user a server admin</label>'
        '<button type="submit">Create user</button></form>'
        f'<p class="note" style="margin-top:.8rem">ID will be '
        f'<code>@username:{_e(settings.name)}</code>.</p></div>'
    )
    return _page(request, "New user", "/console/users", body)


@router.post("/console/users/new", include_in_schema=False)
async def user_new_submit(
    request: Request,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    localpart: str = Form(...),
    password: str = Form(...),
    displayname: str = Form(""),
    make_admin: bool = Form(False),
) -> Response:
    settings = _settings(request)
    admin = _admin(request)
    localpart = localpart.strip()
    if not localpart:
        return RedirectResponse("/console/users/new?error=Username+is+required", status_code=303)
    user_id = _full_user_id(settings, localpart)
    if await accounts.get_user(request.app.state.db, user_id) is not None:
        return RedirectResponse(
            "/console/users/new?error=That+username+is+already+taken", status_code=303
        )
    await admin.upsert_user(
        user_id,
        {"password": password, "admin": make_admin, "displayname": displayname or None},
    )
    _flash(request, f"Created {user_id}.")
    return RedirectResponse(f"/console/users/{_quote(user_id)}", status_code=303)


@router.get("/console/users/{user_id}", include_in_schema=False)
async def user_detail(
    request: Request, user_id: str, _: str = Depends(require_console_admin)
) -> Response:
    admin = _admin(request)
    user = await admin.get_user(user_id)
    is_admin = bool(user.get("admin"))
    deactivated = bool(user.get("deactivated"))
    csrf = _csrf_field(request)
    quoted = _quote(user_id)

    created = _fmt_ts(int(user.get("creation_ts") or 0)) if user.get("creation_ts") else "—"
    info = (
        '<dl class="kv">'
        f"<dt>User ID</dt><dd>{_e(user_id)}</dd>"
        f'<dt>Display name</dt><dd>{_e(user.get("displayname") or "—")}</dd>'
        f"<dt>Role</dt><dd>{_pill(is_admin, 'admin', 'user')}</dd>"
        f"<dt>Status</dt><dd>{_pill(not deactivated, 'active', 'deactivated')}</dd>"
        f"<dt>Created</dt><dd>{created}</dd></dl>"
    )

    admin_btn = "Revoke admin" if is_admin else "Grant admin"
    admin_value = "false" if is_admin else "true"
    actions = (
        '<div class="panel"><h2>Profile</h2>'
        f'<form method="post" action="/console/users/{quoted}/profile">{csrf}'
        '<label for="dn">Display name</label>'
        f'<input id="dn" name="displayname" value="{_e(user.get("displayname") or "")}"'
        ' placeholder="(none)">'
        '<button class="btn" type="submit">Save display name</button></form></div>'
        '<div class="panel"><h2>Role</h2>'
        f'<form method="post" action="/console/users/{quoted}/admin">{csrf}'
        f'<input type="hidden" name="admin" value="{admin_value}">'
        f'<button class="btn" type="submit">{admin_btn}</button></form></div>'
        '<div class="panel"><h2>Reset password</h2>'
        f'<form method="post" action="/console/users/{quoted}/reset-password">{csrf}'
        '<input name="new_password" type="password" placeholder="New password" required>'
        '<button class="btn" type="submit">Reset password</button></form></div>'
    )
    if not deactivated:
        actions += (
            '<div class="panel"><h2>Deactivate</h2>'
            '<p class="muted" style="margin-bottom:.7rem">Disables the account and revokes '
            "its access tokens.</p>"
            f'<form method="post" action="/console/users/{quoted}/deactivate">{csrf}'
            '<button class="btn danger" type="submit">Deactivate account</button></form></div>'
        )
    else:
        actions += (
            '<div class="panel"><h2>Reactivate</h2>'
            '<p class="muted" style="margin-bottom:.7rem">Re-enables this account. The user '
            "can sign in again (they keep their password).</p>"
            f'<form method="post" action="/console/users/{quoted}/reactivate">{csrf}'
            '<button class="btn" type="submit">Reactivate account</button></form></div>'
        )
    shadow_banned = bool(user.get("shadow_banned"))
    sb_label = "Remove shadow-ban" if shadow_banned else "Shadow-ban"
    sb_value = "false" if shadow_banned else "true"
    sb_pill = _pill(shadow_banned, "on", "off")
    actions += (
        '<div class="panel"><h2>Moderation</h2>'
        f'<p class="note" style="margin-bottom:.8rem">Shadow-ban {sb_pill}'
        " — a shadow-banned user can still post, but no one else sees their messages.</p>"
        '<div class="row">'
        f'<form class="inline" method="post" action="/console/users/{quoted}/shadow-ban">{csrf}'
        f'<input type="hidden" name="banned" value="{sb_value}">'
        f'<button class="btn" type="submit">{sb_label}</button></form>'
        f'<form class="inline" method="post" action="/console/users/{quoted}/redact" '
        "onsubmit=\"return confirm('Redact all messages from this user?')\">"
        f'{csrf}<button class="btn danger" type="submit">Redact all messages</button></form>'
        "</div></div>"
        '<div class="panel"><h2>Send server notice</h2>'
        f'<form method="post" action="/console/users/{quoted}/notice">{csrf}'
        '<input name="message" placeholder="A message delivered to this user" required>'
        '<button class="btn" type="submit">Send notice</button></form></div>'
    )
    body = (
        f'<h1 class="page">{_e(user_id)}</h1>'
        f'<div class="panel">{info}</div>{actions}'
        '<p class="muted"><a href="/console/users">&larr; All users</a></p>'
    )
    return _page(request, user_id, "/console/users", body)


@router.post("/console/users/{user_id}/shadow-ban", include_in_schema=False)
async def user_shadow_ban(
    request: Request,
    user_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    banned: bool = Form(False),
) -> Response:
    await _admin(request).set_shadow_ban(user_id, banned)
    _flash(request, "Shadow-banned." if banned else "Shadow-ban removed.")
    return RedirectResponse(f"/console/users/{_quote(user_id)}", status_code=303)


@router.post("/console/users/{user_id}/redact", include_in_schema=False)
async def user_redact(
    request: Request,
    user_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
) -> Response:
    result = await _admin(request).redact_user_events(user_id)
    await _admin(request).get_redact_status(result["redact_id"])
    _flash(request, "Redacted the user's messages.")
    return RedirectResponse(f"/console/users/{_quote(user_id)}", status_code=303)


@router.post("/console/users/{user_id}/notice", include_in_schema=False)
async def user_server_notice(
    request: Request,
    user_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    message: str = Form(...),
) -> Response:
    await _admin(request).send_server_notice(
        user_id, {"msgtype": "m.text", "body": message}
    )
    _flash(request, "Server notice sent.")
    return RedirectResponse(f"/console/users/{_quote(user_id)}", status_code=303)


@router.post("/console/users/{user_id}/admin", include_in_schema=False)
async def user_set_admin(
    request: Request,
    user_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    admin: bool = Form(False),
) -> Response:
    await _admin(request).upsert_user(user_id, {"admin": admin})
    _flash(request, "Granted admin." if admin else "Revoked admin.")
    return RedirectResponse(f"/console/users/{_quote(user_id)}", status_code=303)


@router.post("/console/users/{user_id}/reset-password", include_in_schema=False)
async def user_reset_password(
    request: Request,
    user_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    new_password: str = Form(...),
) -> Response:
    await _admin(request).reset_password(user_id, new_password)
    _flash(request, "Password reset.")
    return RedirectResponse(f"/console/users/{_quote(user_id)}", status_code=303)


@router.post("/console/users/{user_id}/deactivate", include_in_schema=False)
async def user_deactivate(
    request: Request,
    user_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
) -> Response:
    await _admin(request).deactivate_user(user_id)
    _flash(request, f"Deactivated {user_id}.")
    return RedirectResponse(f"/console/users/{_quote(user_id)}", status_code=303)


@router.post("/console/users/{user_id}/reactivate", include_in_schema=False)
async def user_reactivate(
    request: Request,
    user_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
) -> Response:
    await _admin(request).upsert_user(user_id, {"deactivated": False})
    _flash(request, f"Reactivated {user_id}.")
    return RedirectResponse(f"/console/users/{_quote(user_id)}", status_code=303)


@router.post("/console/users/{user_id}/profile", include_in_schema=False)
async def user_set_profile(
    request: Request,
    user_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    displayname: str = Form(""),
) -> Response:
    await _admin(request).upsert_user(user_id, {"displayname": displayname})
    _flash(request, "Display name updated.")
    return RedirectResponse(f"/console/users/{_quote(user_id)}", status_code=303)


# --- rooms ------------------------------------------------------------------
@router.get("/console/rooms", include_in_schema=False)
async def rooms_list(
    request: Request, _: str = Depends(require_console_admin), offset: int = 0
) -> Response:
    admin = _admin(request)
    offset = max(0, offset)
    page = await admin.list_rooms(offset=offset, limit=_PAGE_SIZE)
    rows = ""
    for r in page.get("rooms", []):
        rid = str(r.get("room_id", ""))
        link = f"/console/rooms/{_quote(rid)}"
        rows += (
            f'<tr><td class="check"><input type="checkbox" class="rowcheck"'
            f' name="room_ids" value="{_e(rid)}"{_BULK_ONCHANGE}></td>'
            f'<td><a href="{link}">{_e(r.get("name") or rid)}</a>'
            f'<div class="muted">{_e(rid)}</div></td>'
            f'<td>{r.get("joined_members", 0)}</td>'
            f'<td>{_pill(bool(r.get("encryption")), "encrypted", "plain")}</td>'
            f'<td>{_e(r.get("version") or "")}</td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="5" class="muted">No rooms yet.</td></tr>'
    total = page.get("total_rooms", 0)
    nav = _pager("/console/rooms", offset, _PAGE_SIZE, total, "")
    body = (
        f'<h1 class="page">Rooms <span class="muted">({total})</span></h1>'
        '<form class="bulk-form" method="post" action="/console/rooms/bulk">'
        f"{_csrf_field(request)}"
        '<div class="bulkbar"><strong><span data-count>0</span> selected</strong>'
        '<button class="btn sm" name="action" value="block">Block</button>'
        '<button class="btn sm ghost" name="action" value="unblock">Unblock</button>'
        '<button class="btn sm danger" name="action" value="delete">'
        "Delete &amp; purge</button></div>"
        '<div class="panel"><table class="tbl"><thead><tr>'
        '<th class="check"><input type="checkbox" class="checkall"'
        ' onchange="neuronCheckAll(this)"></th>'
        '<th>Room</th><th>Members</th><th>Encryption</th><th>Version</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>{nav}</div></form>"
    )
    return _page(request, "Rooms", "/console/rooms", body)


@router.post("/console/rooms/bulk", include_in_schema=False)
async def rooms_bulk(
    request: Request,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    action: str = Form(""),
    room_ids: list[str] = Form([]),
) -> Response:
    admin = _admin(request)
    done = 0
    for rid in room_ids:
        try:
            if action == "block":
                await admin.set_room_block(rid, True)
            elif action == "unblock":
                await admin.set_room_block(rid, False)
            elif action == "delete":
                await admin.delete_room(rid)
            else:
                break
            done += 1
        except Exception:  # noqa: BLE001 - best-effort bulk; skip per-item failures
            continue
    labels = {"block": "Blocked", "unblock": "Unblocked", "delete": "Deleted"}
    _flash(request, f"{labels.get(action, 'Updated')} {done} room(s).")
    return RedirectResponse("/console/rooms", status_code=303)


@router.get("/console/rooms/{room_id}", include_in_schema=False)
async def room_detail(
    request: Request, room_id: str, _: str = Depends(require_console_admin)
) -> Response:
    admin = _admin(request)
    room = await admin.get_room(room_id)
    members = await admin.get_room_members(room_id)
    state = await admin.get_room_state(room_id)
    info = (
        '<dl class="kv">'
        f"<dt>Room ID</dt><dd>{_e(room_id)}</dd>"
        f'<dt>Name</dt><dd>{_e(room.get("name") or "—")}</dd>'
        f'<dt>Creator</dt><dd>{_e(room.get("creator") or "—")}</dd>'
        f'<dt>Members</dt><dd>{members.get("total", 0)}</dd>'
        f'<dt>Version</dt><dd>{_e(room.get("version") or "—")}</dd>'
        f'<dt>Encryption</dt><dd>{_e(room.get("encryption") or "off")}</dd>'
        f'<dt>Join rule</dt><dd>{_e(room.get("join_rules") or "—")}</dd>'
        f'<dt>State events</dt><dd>{room.get("state_events", len(state.get("state", [])))}</dd>'
        "</dl>"
    )
    member_items = "".join(f"<li>{_e(m)}</li>" for m in members.get("members", []))
    members_panel = (
        f'<div class="panel"><h2>Members ({members.get("total", 0)})</h2>'
        '<ul class="muted" style="columns:2;margin:0;padding-left:1.1rem">'
        f"{member_items}</ul></div>"
        if member_items
        else ""
    )
    blocked = await admin.is_room_blocked(room_id)
    csrf = _csrf_field(request)
    quoted = _quote(room_id)
    block_label = "Unblock room" if blocked else "Block room"
    block_value = "false" if blocked else "true"
    mod = (
        '<div class="panel"><h2>Moderation</h2>'
        f'<p class="note" style="margin-bottom:.8rem">Block {_pill(blocked, "on", "off")}'
        " — a blocked room rejects all sends and joins.</p>"
        '<div class="row">'
        f'<form class="inline" method="post" action="/console/rooms/{quoted}/block">{csrf}'
        f'<input type="hidden" name="block" value="{block_value}">'
        f'<button class="btn" type="submit">{block_label}</button></form>'
        f'<form class="inline" method="post" action="/console/rooms/{quoted}/delete" '
        "onsubmit=\"return confirm('Delete and purge this room? This cannot be undone.')\">"
        f"{csrf}"
        '<label class="muted" style="margin:0 .4rem"><input type="checkbox" name="purge" '
        'value="true" checked style="width:auto"> purge</label>'
        '<button class="btn danger" type="submit">Delete room</button></form>'
        "</div></div>"
    )
    body = (
        f'<h1 class="page">{_e(room.get("name") or room_id)}</h1>'
        f'<div class="panel">{info}</div>{members_panel}{mod}'
        '<p class="muted"><a href="/console/rooms">&larr; All rooms</a></p>'
    )
    return _page(request, "Room", "/console/rooms", body)


@router.post("/console/rooms/{room_id}/block", include_in_schema=False)
async def room_block(
    request: Request,
    room_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    block: bool = Form(False),
) -> Response:
    await _admin(request).set_room_block(room_id, block)
    _flash(request, "Room blocked." if block else "Room unblocked.")
    return RedirectResponse(f"/console/rooms/{_quote(room_id)}", status_code=303)


@router.post("/console/rooms/{room_id}/delete", include_in_schema=False)
async def room_delete(
    request: Request,
    room_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    purge: bool = Form(False),
) -> Response:
    await _admin(request).delete_room(room_id, purge=purge, block=not purge)
    _flash(request, "Room deleted.")
    return RedirectResponse("/console/rooms", status_code=303)


# --- invites / registration tokens -----------------------------------------
def _invite_url(settings: NeuronServerSettings, token: str) -> str:
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/get-started?token={_quote(token)}"


@router.get("/console/invites", include_in_schema=False)
async def invites_list(
    request: Request, _: str = Depends(require_console_admin)
) -> Response:
    settings = _settings(request)
    tokens = (await _admin(request).list_registration_tokens()).get("registration_tokens", [])
    rows = ""
    for t in tokens:
        tok = str(t.get("token", ""))
        url = _invite_url(settings, tok)
        uses = "∞" if t.get("uses_allowed") is None else str(t.get("uses_allowed"))
        rows += (
            f'<tr><td><code>{_e(tok)}</code></td>'
            f'<td>{t.get("completed", 0)} / {uses}</td>'
            f'<td><a href="{_e(url)}">invite link</a> '
            f'&middot; <a href="/console/invites/{_quote(tok)}/qr.svg">QR</a></td>'
            '<td><form class="inline" method="post" '
            f'action="/console/invites/{_quote(tok)}/delete">'
            f'{_csrf_field(request)}<button class="btn sm danger" type="submit">Delete</button>'
            "</form></td></tr>"
        )
    if not rows:
        rows = '<tr><td colspan="4" class="muted">No invite tokens yet.</td></tr>'
    create = (
        '<div class="panel" style="max-width:460px"><h2>New invite</h2>'
        '<form class="row" method="post" action="/console/invites/new">'
        f"{_csrf_field(request)}"
        '<input class="q" name="uses_allowed" placeholder="Uses (blank = unlimited)">'
        '<button class="btn" type="submit">Create</button></form>'
        '<p class="note" style="margin-top:.7rem">Share the link or QR; anyone with it can '
        "create one account.</p></div>"
    )
    body = (
        '<h1 class="page">Invites</h1>'
        '<div class="panel"><table class="tbl"><thead><tr><th>Token</th><th>Used</th>'
        f"<th>Share</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>{create}"
    )
    return _page(request, "Invites", "/console/invites", body)


@router.post("/console/invites/new", include_in_schema=False)
async def invites_new(
    request: Request,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    uses_allowed: str = Form(""),
) -> Response:
    uses = int(uses_allowed) if uses_allowed.strip().isdigit() else None
    await _admin(request).create_registration_token(
        token=None, uses_allowed=uses, expiry_time=None
    )
    _flash(request, "Invite created.")
    return RedirectResponse("/console/invites", status_code=303)


@router.post("/console/invites/{token}/delete", include_in_schema=False)
async def invites_delete(
    request: Request,
    token: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
) -> Response:
    await _admin(request).delete_registration_token(token)
    _flash(request, "Invite deleted.")
    return RedirectResponse("/console/invites", status_code=303)


@router.get("/console/invites/{token}/qr.svg", include_in_schema=False)
async def invite_qr(
    request: Request, token: str, _: str = Depends(require_console_admin)
) -> Response:
    import segno

    url = _invite_url(_settings(request), token)
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=5, border=2)
    return Response(buf.getvalue(), media_type="image/svg+xml")


# --- settings + doctor ------------------------------------------------------
_DOCTOR_PILL = {"ok": "on", "warn": "amber", "fail": "warn"}


def _save_desktop_config_key(path: str, key: str, value: Any) -> None:
    """Update one key in the desktop app's flat config.json (no neuron_desktop import)."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data[key] = value
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@router.get("/console/settings", include_in_schema=False)
async def settings_page(
    request: Request, _: str = Depends(require_console_admin), net: int = 0
) -> Response:
    from neuron_server import doctor  # local import: pulls federation/network deps

    settings = _settings(request)
    db = request.app.state.db
    committed = await metadata.get_metadata(db, "server_name") or settings.name
    editable = bool(settings.desktop_config_path)

    # Identity (server name is permanent — shown read-only with an explanation).
    identity = (
        '<div class="panel"><h2>Identity</h2>'
        '<dl class="kv">'
        f"<dt>Server name</dt><dd>{_e(committed)}</dd>"
        f"<dt>Public URL</dt><dd>{_e(settings.public_base_url)}</dd>"
        f"<dt>Listening on</dt><dd>{_e(settings.bind_host)}:{settings.bind_port}</dd></dl>"
        '<p class="note" style="margin-top:.7rem">Your server\'s name is permanent — it is '
        "built into every account, room and message, so it cannot be changed after the server "
        "starts. To use a different name, set it in the desktop <em>Settings…</em> window "
        "before first run, or create a new server.</p></div>"
    )

    # Registration (editable when run by the desktop app).
    checked = " checked" if settings.registration_enabled else ""
    if editable:
        registration = (
            '<div class="panel"><h2>Registration</h2>'
            '<form method="post" action="/console/settings">'
            f"{_csrf_field(request)}"
            '<label class="row" style="font-weight:400;margin:.2rem 0 1rem">'
            f'<input type="checkbox" name="registration_enabled" value="true"{checked} '
            'style="width:auto;margin:0 .5rem 0 0"> Allow open registration '
            "(anyone can create an account)</label>"
            '<button class="btn" type="submit">Save</button></form>'
            '<p class="note" style="margin-top:.6rem">Changes take effect after a server '
            "restart (Neuron tray menu &rarr; <em>Restart server</em>).</p></div>"
        )
    else:
        state = "open" if settings.registration_enabled else "closed (invite-only)"
        registration = (
            f'<div class="panel"><h2>Registration</h2><p class="note">Registration is '
            f"<strong>{state}</strong>. Editing settings here needs the desktop app; set "
            "<code>NEURON_SERVER_REGISTRATION_ENABLED</code> in the environment "
            "otherwise.</p></div>"
        )

    # Doctor — structured health checks rendered inline.
    checks = await doctor.run_checks(settings, offline=(net == 0))
    rows = ""
    for c in checks:
        status = str(c.status)
        rows += (
            f'<tr><td>{_e(c.name)}</td>'
            f'<td><span class="pill {_DOCTOR_PILL.get(status, "off")}">{_e(status)}</span></td>'
            f"<td>{_e(c.detail)}</td></tr>"
        )
    n_fail = sum(1 for c in checks if str(c.status) == "fail")
    n_warn = sum(1 for c in checks if str(c.status) == "warn")
    n_ok = sum(1 for c in checks if str(c.status) == "ok")
    toggle = (
        '<a class="btn sm ghost" href="/console/settings">Quick checks only</a>'
        if net
        else '<a class="btn sm ghost" href="/console/settings?net=1">Include network checks</a>'
    )
    doctor_panel = (
        '<div class="panel"><div class="spread"><h2>Health check</h2>'
        f"{toggle}</div>"
        f'<p class="muted">{n_ok} ok &middot; {n_warn} warning(s) &middot; {n_fail} failure(s)</p>'
        '<table class="tbl"><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )

    body = f'<h1 class="page">Server settings</h1>{identity}{registration}{doctor_panel}'
    return _page(request, "Server settings", "/console/settings", body)


@router.post("/console/settings", include_in_schema=False)
async def settings_save(
    request: Request,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    registration_enabled: bool = Form(False),
) -> Response:
    settings = _settings(request)
    if settings.desktop_config_path:
        _save_desktop_config_key(
            settings.desktop_config_path, "registration_enabled", registration_enabled
        )
        _flash(
            request,
            "Settings saved. Restart the server to apply "
            "(Neuron tray menu → Restart server).",
        )
    else:
        _flash(request, "This server is not managed by the desktop app; settings were not saved.")
    return RedirectResponse("/console/settings", status_code=303)


# --- passkeys (WebAuthn) ----------------------------------------------------
# Client-side enrolment + login ceremony. py_webauthn emits/consumes base64url for
# binary fields, so this converts to/from ArrayBuffers. Inlined (the console serves
# no static files). Wires the #pk-add (passkeys page) and #pk-login (login) buttons.
_PASSKEY_JS = """<script>
(function(){
"use strict";
function b2b(s){
  s=s.replace(/-/g,'+').replace(/_/g,'/');
  while(s.length%4)s+='=';
  var b=atob(s),u=new Uint8Array(b.length);
  for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i);
  return u.buffer;
}
function f2b(buf){
  var by=new Uint8Array(buf),s='';
  for(var i=0;i<by.length;i++)s+=String.fromCharCode(by[i]);
  return btoa(s).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
}
function err(id,m){
  var e=id&&document.getElementById(id);
  if(e){e.textContent=m;e.hidden=false;}else{alert(m);}
}
async function pj(url,body,h){
  var r=await fetch(url,{
    method:'POST',
    headers:Object.assign({'Content-Type':'application/json'},h||{}),
    body:body?JSON.stringify(body):'{}',
    credentials:'same-origin'
  });
  var d={};
  try{d=await r.json();}catch(e){}
  if(!r.ok)throw new Error((d&&d.error)||('Request failed ('+r.status+')'));
  return d;
}
function sc(c){
  var r=c.response;
  return{id:c.id,rawId:f2b(c.rawId),type:c.type,response:{
    clientDataJSON:f2b(r.clientDataJSON),
    attestationObject:f2b(r.attestationObject),
    transports:r.getTransports?r.getTransports():[]
  },clientExtensionResults:c.getClientExtensionResults()};
}
function sg(c){
  var r=c.response;
  return{id:c.id,rawId:f2b(c.rawId),type:c.type,response:{
    clientDataJSON:f2b(r.clientDataJSON),
    authenticatorData:f2b(r.authenticatorData),
    signature:f2b(r.signature),
    userHandle:r.userHandle?f2b(r.userHandle):null
  },clientExtensionResults:c.getClientExtensionResults()};
}
window.neuronPasskeyRegister=async function(label,eid){
  if(!window.PublicKeyCredential)
    return err(eid,'This browser does not support passkeys.');
  try{
    var csrf=window.NEURON_CSRF||'';
    var o=await pj('/console/passkeys/register/options',{},{'X-CSRF-Token':csrf});
    o.challenge=b2b(o.challenge);
    o.user.id=b2b(o.user.id);
    (o.excludeCredentials||[]).forEach(function(c){c.id=b2b(c.id);});
    var cred=await navigator.credentials.create({publicKey:o});
    await pj('/console/passkeys/register/verify',
      {credential:sc(cred),label:label},{'X-CSRF-Token':csrf});
    window.location.reload();
  }catch(e){err(eid,e.message||String(e));}
};
window.neuronPasskeyLogin=async function(eid){
  if(!window.PublicKeyCredential)
    return err(eid,'This browser does not support passkeys.');
  try{
    var o=await pj('/console/passkeys/login/options',{});
    o.challenge=b2b(o.challenge);
    (o.allowCredentials||[]).forEach(function(c){c.id=b2b(c.id);});
    var cred=await navigator.credentials.get({publicKey:o});
    await pj('/console/passkeys/login/verify',{credential:sg(cred)});
    window.location.assign('/console');
  }catch(e){err(eid,e.message||String(e));}
};
document.addEventListener('DOMContentLoaded',function(){
  var a=document.getElementById('pk-add');
  if(a)a.addEventListener('click',function(){
    neuronPasskeyRegister((document.getElementById('pk-label')||{}).value||'','pk-err');
  });
  var l=document.getElementById('pk-login');
  if(l)l.addEventListener('click',function(){neuronPasskeyLogin('pk-login-err');});
});
})();
</script>"""


def _rp(request: Request) -> tuple[str, str]:
    """Resolve the WebAuthn relying-party id + expected origin for this request."""
    settings = _settings(request)
    rp_id = settings.webauthn_rp_id or (request.url.hostname or "localhost")
    origin = settings.webauthn_origin or f"{request.url.scheme}://{request.url.netloc}"
    return rp_id, origin


async def _owner_is_admin(request: Request, user_id: str) -> bool:
    settings = _settings(request)
    if user_id in settings.admin_user_ids():
        return True
    row = await accounts.get_user(request.app.state.db, user_id)
    return bool(row and row.admin and not row.deactivated)


def _passkey_script(request: Request) -> str:
    csrf = json.dumps(get_csrf_token(request))
    return f"<script>window.NEURON_CSRF={csrf};</script>{_PASSKEY_JS}"


@router.get("/console/passkeys", include_in_schema=False)
async def passkeys_page(request: Request, who: str = Depends(require_console_admin)) -> Response:
    keys = await admin_store.list_passkeys(request.app.state.db, who)
    rows = ""
    for k in keys:
        rows += (
            f'<tr><td>{_e(str(k["label"]))}</td>'
            '<td><form class="inline" method="post" action="/console/passkeys/delete">'
            f'{_csrf_field(request)}'
            f'<input type="hidden" name="credential_id" value="{_e(str(k["credential_id"]))}">'
            '<button class="btn sm danger" type="submit">Remove</button></form></td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="2" class="muted">No passkeys yet.</td></tr>'
    body = (
        '<h1 class="page">Passkeys</h1>'
        '<div class="panel"><p class="note">Sign in to the console with a device passkey '
        "(Touch ID, a security key, your phone) instead of a password.</p>"
        '<div class="row" style="margin:.7rem 0">'
        '<input class="q" id="pk-label" placeholder="Label (e.g. MacBook)">'
        '<button class="btn" id="pk-add" type="button">Add a passkey</button></div>'
        '<div class="error" id="pk-err" hidden></div>'
        '<table class="tbl"><thead><tr><th>Passkey</th><th></th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>{_passkey_script(request)}"
    )
    return _page(request, "Passkeys", "/console/passkeys", body)


@router.post("/console/passkeys/register/options", include_in_schema=False)
async def passkey_register_options(
    request: Request, who: str = Depends(require_console_admin)
) -> Response:
    if not verify_csrf(request, request.headers.get("x-csrf-token", "")):
        raise CsrfError()
    from neuron_server import passkeys as pk

    rp_id, _origin = _rp(request)
    keys = await admin_store.list_passkeys(request.app.state.db, who)
    exclude = [k["credential_id"] for k in keys]
    options_json, challenge = pk.registration_options(rp_id, user_id=who, exclude_ids=exclude)
    request.session["webauthn_reg_challenge"] = challenge
    return Response(options_json, media_type="application/json")


@router.post("/console/passkeys/register/verify", include_in_schema=False)
async def passkey_register_verify(
    request: Request, who: str = Depends(require_console_admin)
) -> Response:
    if not verify_csrf(request, request.headers.get("x-csrf-token", "")):
        raise CsrfError()
    from neuron_server import passkeys as pk

    body = await request.json()
    challenge = str(request.session.pop("webauthn_reg_challenge", ""))
    rp_id, origin = _rp(request)
    try:
        cred = pk.verify_registration(
            json.dumps(body["credential"]), challenge, rp_id, origin,
            label=str(body.get("label", "")),
        )
    except Exception as exc:  # noqa: BLE001 - report verification failure to the UI
        return JSONResponse({"error": str(exc)}, status_code=400)
    await admin_store.add_passkey(
        request.app.state.db,
        credential_id=str(cred["credential_id"]),
        owner=who,
        public_key=str(cred["public_key"]),
        sign_count=int(cred["sign_count"]),
        label=str(cred["label"]),
        ts=int(cred["created_ts"]),
    )
    return JSONResponse({"ok": True})


@router.post("/console/passkeys/delete", include_in_schema=False)
async def passkey_delete(
    request: Request,
    who: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
    credential_id: str = Form(...),
) -> Response:
    await admin_store.remove_passkey(request.app.state.db, who, credential_id)
    _flash(request, "Passkey removed.")
    return RedirectResponse("/console/passkeys", status_code=303)


@router.post("/console/passkeys/login/options", include_in_schema=False)
async def passkey_login_options(request: Request) -> Response:
    from neuron_server import passkeys as pk

    rp_id, _origin = _rp(request)
    allow = await admin_store.all_passkey_ids(request.app.state.db)
    options_json, challenge = pk.authentication_options(rp_id, allow_ids=allow)
    request.session["webauthn_login_challenge"] = challenge
    return Response(options_json, media_type="application/json")


@router.post("/console/passkeys/login/verify", include_in_schema=False)
async def passkey_login_verify(request: Request) -> Response:
    from neuron_server import passkeys as pk

    body = await request.json()
    challenge = str(request.session.pop("webauthn_login_challenge", ""))
    rp_id, origin = _rp(request)
    credential = json.dumps(body["credential"])
    try:
        stored = await admin_store.get_passkey(
            request.app.state.db, pk.credential_id_of(credential)
        )
        if stored is None:
            raise ValueError("Unknown passkey")
        if not await _owner_is_admin(request, str(stored["owner"])):
            raise ValueError("That account is not a server administrator")
        new_count = pk.verify_authentication(
            credential, challenge, rp_id, origin,
            public_key=str(stored["public_key"]), sign_count=int(stored["sign_count"]),
        )
    except Exception as exc:  # noqa: BLE001 - any failure is an auth failure
        return JSONResponse({"error": str(exc)}, status_code=400)
    await admin_store.set_passkey_sign_count(
        request.app.state.db, str(stored["credential_id"]), new_count
    )
    request.session["console_user"] = str(stored["owner"])
    return JSONResponse({"ok": True})


# --- reports ----------------------------------------------------------------
_REPORTS_PER_PAGE = 25


@router.get("/console/reports", include_in_schema=False)
async def reports_page(
    request: Request, _: str = Depends(require_console_admin), offset: int = 0
) -> Response:
    offset = max(0, offset)
    data = await _admin(request).list_event_reports(offset=offset, limit=_REPORTS_PER_PAGE)
    total = int(data.get("total", 0))
    rows = ""
    for r in data.get("event_reports", []):
        rid = str(r.get("room_id", ""))
        report_url = f'/console/reports/{_quote(str(r.get("id", "")))}'
        rows += (
            f'<tr><td><a href="{report_url}">{_e(str(r.get("user_id", "")))}</a></td>'
            f'<td><a href="/console/rooms/{_quote(rid)}">{_e(rid)}</a></td>'
            f'<td><code>{_e(str(r.get("event_id", "")))}</code></td>'
            f'<td>{_e(r.get("reason") or "")}</td>'
            f'<td><a class="btn sm ghost" href="{report_url}">Review</a></td></tr>'
        )
    if not rows:
        rows = (
            '<tr><td colspan="5"><div class="empty"><div class="big">No reports</div>'
            "Nothing has been reported on this server.</div></td></tr>"
        )
    body = (
        f'<h1 class="page">Reports <span class="muted">({total})</span></h1>'
        '<div class="panel"><table class="tbl"><thead><tr><th>Reporter</th><th>Room</th>'
        f"<th>Event</th><th>Reason</th><th></th></tr></thead><tbody>{rows}</tbody></table>"
        f'{_pager("/console/reports", offset, _REPORTS_PER_PAGE, total, "")}</div>'
    )
    return _page(request, "Reports", "/console/reports", body)


@router.get("/console/reports/{report_id}", include_in_schema=False)
async def report_detail(
    request: Request, report_id: str, _: str = Depends(require_console_admin)
) -> Response:
    admin = _admin(request)
    report = await admin.get_event_report(report_id)  # 404 if unknown
    room_id = str(report.get("room_id", ""))
    event_id = str(report.get("event_id", ""))
    event = await admin.get_event(room_id, event_id)
    if event is not None:
        content = json.dumps(event.get("content", {}), indent=2, ensure_ascii=False)
        event_html = (
            '<dl class="kv">'
            f'<dt>Sender</dt><dd>{_e(str(event.get("sender", "")))}</dd>'
            f'<dt>Type</dt><dd>{_e(str(event.get("type", "")))}</dd></dl>'
            f'<pre class="codeblock">{_e(content)}</pre>'
        )
    else:
        event_html = (
            '<p class="muted">The reported event is no longer available '
            "(deleted, redacted, or purged).</p>"
        )
    score = report.get("score")
    body = (
        '<div class="spread"><h1 class="page">Report</h1>'
        '<a class="btn sm ghost" href="/console/reports">&larr; All reports</a></div>'
        f'<div class="panel"><h2>Reported by {_e(str(report.get("user_id", "")))}</h2>'
        '<dl class="kv">'
        f'<dt>Room</dt><dd><a href="/console/rooms/{_quote(room_id)}">{_e(room_id)}</a></dd>'
        f'<dt>Event</dt><dd><code>{_e(event_id)}</code></dd>'
        f'<dt>Reason</dt><dd>{_e(report.get("reason") or "—")}</dd>'
        f'<dt>Score</dt><dd>{_e(str(score)) if score is not None else "—"}</dd>'
        f'<dt>Received</dt><dd>{_e(_fmt_ts(int(report.get("received_ts", 0))))}</dd></dl>'
        f'<form class="inline" method="post" action="/console/reports/{_quote(report_id)}/dismiss">'
        f'{_csrf_field(request)}<button class="btn danger">Dismiss report</button></form></div>'
        f'<div class="panel"><h2>Reported event</h2>{event_html}</div>'
    )
    return _page(request, "Report", "/console/reports", body)


@router.post("/console/reports/{report_id}/dismiss", include_in_schema=False)
async def report_dismiss(
    request: Request,
    report_id: str,
    _: str = Depends(require_console_admin),
    __: None = Depends(csrf_protect),
) -> Response:
    await _admin(request).delete_event_report(report_id)
    _flash(request, "Report dismissed.")
    return RedirectResponse("/console/reports", status_code=303)


# --- pagination helper ------------------------------------------------------
def _pager(path: str, offset: int, limit: int, total: int, q: str, extra: str = "") -> str:
    if total <= limit:
        return ""
    qs = (f"&q={_quote(q)}" if q else "") + extra
    parts: list[str] = []
    if offset > 0:
        prev = max(0, offset - limit)
        parts.append(f'<a class="btn sm ghost" href="{path}?offset={prev}{qs}">&larr; Prev</a>')
    if offset + limit < total:
        nxt = offset + limit
        parts.append(f'<a class="btn sm ghost" href="{path}?offset={nxt}{qs}">Next &rarr;</a>')
    inner = "".join(parts)
    return f'<div class="row" style="margin-top:14px">{inner}</div>' if inner else ""


# --- install ----------------------------------------------------------------
def install(app: Any) -> None:
    """Register the console router and its session exception handlers on ``app``."""
    app.include_router(router)

    @app.exception_handler(NotAuthenticated)
    async def _on_not_authed(request: Request, exc: NotAuthenticated) -> Response:
        return RedirectResponse("/console/login", status_code=303)

    @app.exception_handler(CsrfError)
    async def _on_csrf(request: Request, exc: CsrfError) -> Response:
        settings = _settings(request)
        body = (
            '<h1 class="page">Session expired</h1>'
            '<div class="panel"><p class="note">Your session expired or the form token was '
            'invalid. Please <a href="/console/login">sign in</a> and try again.</p></div>'
        )
        doc = branding.admin_shell("Error", body, active="", server_name=settings.name)
        return HTMLResponse(doc, status_code=400)
