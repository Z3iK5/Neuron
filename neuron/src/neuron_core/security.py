# SPDX-License-Identifier: Apache-2.0
"""CSRF protection for cookie-session admin consoles (shared by all services).

A console that performs destructive admin actions behind a cookie-based session
must defend against Cross-Site Request Forgery: a malicious page tricking a
logged-in operator's browser into submitting a request. We use the classic
*synchroniser token* pattern:

1. When rendering a form, embed a random token (stored in the session) as a
   hidden field — see ``get_csrf_token``.
2. When handling the POST, check the submitted token matches the session's —
   see ``verify_csrf``. A forged cross-site request cannot read the token, so it
   cannot submit a valid one.

Requires Starlette's ``SessionMiddleware`` (or anything that puts a mutable
mapping on ``request.session``); Starlette ships with both FastAPI services, so
this module stays out of ``neuron_core/__init__`` to keep the base library free
of web-framework imports.
"""

from __future__ import annotations

import secrets

from starlette.requests import Request

_SESSION_KEY = "csrf_token"


def get_csrf_token(request: Request) -> str:
    """Return the session's CSRF token, creating one on first use."""
    token = request.session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_SESSION_KEY] = token
    return str(token)


def verify_csrf(request: Request, submitted: str) -> bool:
    """Return True if ``submitted`` matches the session's CSRF token."""
    expected = request.session.get(_SESSION_KEY)
    return bool(expected) and secrets.compare_digest(submitted, str(expected))
