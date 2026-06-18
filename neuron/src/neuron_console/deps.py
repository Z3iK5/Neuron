# SPDX-License-Identifier: Apache-2.0
"""Shared dependencies and helpers for the console's request handlers.

FastAPI "dependencies" are small functions that run before a route handler and
provide it with something it needs (here: the current settings, an admin-API
client, an authentication check, or CSRF protection). Keeping them here makes
the route code in ``app.py`` short and lets tests override them.
"""

from __future__ import annotations

from fastapi import Form
from starlette.requests import Request

from neuron_console.config import ConsoleSettings
from neuron_console.passkeys import PasskeyStore
from neuron_console.security import verify_csrf
from neuron_core import AdminClient
from neuron_supervisor import Supervisor


class NotAuthenticated(Exception):
    """Raised by ``require_login`` when there is no valid session."""


class CsrfError(Exception):
    """Raised when a state-changing request has a missing/invalid CSRF token."""


class MasDisabledError(Exception):
    """Raised when an action is unavailable because auth is delegated to MAS."""

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(f"{action} is disabled when authentication is delegated to MAS.")


def get_settings(request: Request) -> ConsoleSettings:
    """Return the ConsoleSettings stored on the app at startup."""
    settings: ConsoleSettings = request.app.state.settings
    return settings


def get_admin(request: Request) -> AdminClient:
    """Return the shared homeserver Admin API client.

    Tests override this dependency to inject a fake client, so no real
    homeserver is needed to test the console's request handling.
    """
    admin: AdminClient = request.app.state.admin
    return admin


def get_supervisor(request: Request) -> Supervisor:
    """Return the shared Supervisor (built at startup from the admin + bot clients)."""
    supervisor: Supervisor = request.app.state.supervisor
    return supervisor


def get_passkeys(request: Request) -> PasskeyStore:
    """Return the shared passkey credential store."""
    store: PasskeyStore = request.app.state.passkeys
    return store


def require_login(request: Request) -> None:
    """Ensure the request has an authenticated session, else redirect to login."""
    if not request.session.get("authenticated"):
        raise NotAuthenticated()


async def csrf_protect(request: Request, csrf_token: str = Form(...)) -> None:
    """Reject state-changing requests whose CSRF token doesn't match the session."""
    if not verify_csrf(request, csrf_token):
        raise CsrfError()


def ensure_classic_auth(settings: ConsoleSettings, action: str) -> None:
    """Raise ``MasDisabledError`` if the action is unavailable under MAS."""
    if settings.mas_enabled():
        raise MasDisabledError(action)
