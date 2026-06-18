# SPDX-License-Identifier: Apache-2.0
"""Authenticating inbound federation requests (the server side of X-Matrix).

Shared by all federation endpoints: parses the ``X-Matrix`` header, resolves the
origin server's verify keys (local or remote, via the key resolver), and verifies
the request signature. For requests with a body (POST/PUT) the parsed JSON content
must be supplied so it is included in the verified signature.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from neuron_server.errors import MatrixError
from neuron_server.federation.auth import parse_authorization_header, verify_request


async def authenticate_request(request: Request, *, content: Any | None = None) -> str:
    """Verify the X-Matrix signature on ``request`` and return the origin server."""
    creds = parse_authorization_header(request.headers.get("Authorization", ""))
    if creds is None:
        raise MatrixError(401, "M_UNAUTHORIZED", "Missing or malformed X-Matrix authorization")

    settings = request.app.state.settings
    verify_keys = await request.app.state.server_key_resolver.verify_keys_for(creds.origin)
    if not verify_keys:
        raise MatrixError(
            401, "M_UNAUTHORIZED", f"Could not resolve signing keys for origin {creds.origin!r}"
        )

    uri = request.url.path
    if request.url.query:
        uri += "?" + request.url.query
    if not verify_request(
        creds,
        method=request.method,
        uri=uri,
        destination=settings.name,
        verify_keys=verify_keys,
        content=content,
    ):
        raise MatrixError(401, "M_UNAUTHORIZED", "Federation request signature did not verify")
    return creds.origin
