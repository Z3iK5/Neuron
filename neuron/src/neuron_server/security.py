# SPDX-License-Identifier: Apache-2.0
"""CSRF protection for the admin console's state-changing forms.

The console performs destructive admin actions behind a cookie-based session, so
it must defend against Cross-Site Request Forgery. We use the classic
*synchroniser token* pattern: a random token is stored in the session and embedded
as a hidden field in every form; submissions must echo it back. A cross-site page
cannot read the token, so it cannot forge a valid submission.

This mirrors ``neuron_console.security`` but lives in ``neuron_server`` so the
merged homeserver+console app has no dependency on the standalone console package.
"""

from __future__ import annotations

import secrets

from starlette.requests import Request

_CSRF_KEY = "csrf_token"


def get_csrf_token(request: Request) -> str:
    """Return the session's CSRF token, creating one on first use."""
    token = request.session.get(_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF_KEY] = token
    return str(token)


def verify_csrf(request: Request, submitted: str) -> bool:
    """Return True if ``submitted`` matches the session's CSRF token."""
    expected = request.session.get(_CSRF_KEY)
    return bool(expected) and secrets.compare_digest(submitted, str(expected))
