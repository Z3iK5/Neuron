# SPDX-License-Identifier: Apache-2.0
"""Lightweight CSRF protection for the console's state-changing forms.

Because the console performs destructive admin actions using a cookie-based
session, we must defend against Cross-Site Request Forgery (CSRF): a malicious
page tricking a logged-in operator's browser into submitting a request.

The approach (the classic "synchroniser token" pattern):

1. When rendering a form, embed a random token (stored in the session) as a
   hidden field — see ``get_csrf_token``.
2. When handling the POST, check the submitted token matches the session's —
   see ``verify_csrf``. A forged cross-site request cannot read the token, so it
   cannot submit a valid one.
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
    return token


def verify_csrf(request: Request, submitted: str) -> bool:
    """Return True if ``submitted`` matches the session's CSRF token."""
    expected = request.session.get(_SESSION_KEY)
    return bool(expected) and secrets.compare_digest(submitted, str(expected))
