# SPDX-License-Identifier: Apache-2.0
"""Server-to-server request authentication: the ``X-Matrix`` scheme (HS-7).

Federation requests are authenticated by signing a canonical JSON description of
the request (method, URI, origin, destination and — for requests with a body —
the content) with the origin server's Ed25519 key, and carrying the signature in
an ``Authorization: X-Matrix ...`` header. This module builds and verifies that
header; it is used both to sign our outbound requests and to verify inbound ones.

Built from the Matrix spec ("Authentication" / "Request Authentication").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from neuron_server.crypto.signing import SigningKey, sign_json, verify_signed_json


@dataclass(frozen=True)
class XMatrixCredentials:
    """The fields parsed from an ``X-Matrix`` Authorization header."""

    origin: str
    destination: str | None
    key_id: str
    signature: str


def _signing_payload(
    *, method: str, uri: str, origin: str, destination: str | None, content: Any | None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"method": method.upper(), "uri": uri, "origin": origin}
    if destination is not None:
        payload["destination"] = destination
    if content is not None:
        payload["content"] = content
    return payload


def sign_request(
    *,
    method: str,
    uri: str,
    origin: str,
    destination: str,
    signing_key: SigningKey,
    content: Any | None = None,
) -> str:
    """Return the ``Authorization`` header value for an outbound federation request."""
    payload = _signing_payload(
        method=method, uri=uri, origin=origin, destination=destination, content=content
    )
    signed = sign_json(payload, server_name=origin, signing_key=signing_key)
    signature = signed["signatures"][origin][signing_key.key_id]
    return (
        f'X-Matrix origin="{origin}",destination="{destination}",'
        f'key="{signing_key.key_id}",sig="{signature}"'
    )


def parse_authorization_header(value: str) -> XMatrixCredentials | None:
    """Parse an ``X-Matrix`` Authorization header into its fields, or ``None``."""
    if not value:
        return None
    scheme, _, rest = value.strip().partition(" ")
    if scheme.lower() != "x-matrix" or not rest:
        return None
    fields: dict[str, str] = {}
    for part in rest.split(","):
        name, sep, raw = part.strip().partition("=")
        if not sep:
            continue
        fields[name.strip().lower()] = raw.strip().strip('"')
    origin = fields.get("origin")
    key_id = fields.get("key")
    signature = fields.get("sig")
    if not origin or not key_id or not signature:
        return None
    return XMatrixCredentials(
        origin=origin,
        destination=fields.get("destination") or None,
        key_id=key_id,
        signature=signature,
    )


def verify_request(
    creds: XMatrixCredentials,
    *,
    method: str,
    uri: str,
    destination: str,
    verify_keys: dict[str, str],
    content: Any | None = None,
) -> bool:
    """Verify ``creds`` against the request, using the origin's ``verify_keys``."""
    verify_key = verify_keys.get(creds.key_id)
    if verify_key is None:
        return False
    # A server that sent a destination must have signed it; otherwise sign without.
    payload = _signing_payload(
        method=method,
        uri=uri,
        origin=creds.origin,
        destination=creds.destination if creds.destination is not None else destination,
        content=content,
    )
    payload["signatures"] = {creds.origin: {creds.key_id: creds.signature}}
    return verify_signed_json(
        payload, server_name=creds.origin, verify_key_base64=verify_key, key_id=creds.key_id
    )
