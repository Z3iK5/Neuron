# SPDX-License-Identifier: Apache-2.0
"""``multipart/mixed`` bodies for authenticated federation media (spec v1.11).

Federation media endpoints return a two-part ``multipart/mixed`` body: the first
part is a JSON metadata object (may be empty ``{}``), the second is the raw media
with its own ``Content-Type`` (and an optional ``Content-Disposition``). We build
these by hand (the exact boundary/CRLF framing matters) and parse the peer's
response the same way — no external multipart dependency.
"""

from __future__ import annotations

import json
import secrets

_CRLF = b"\r\n"


def build_multipart(
    metadata: dict[str, object],
    content_type: str,
    data: bytes,
    disposition: str | None = None,
) -> tuple[str, bytes]:
    """Return ``(boundary, body)`` for a metadata + media ``multipart/mixed`` body.

    The boundary is random so it can never appear inside the media bytes.
    """
    boundary = "neuron" + secrets.token_hex(16)
    delim = b"--" + boundary.encode("ascii")

    body = bytearray()
    # Part 1: JSON metadata.
    body += delim + _CRLF
    body += b"Content-Type: application/json" + _CRLF + _CRLF
    body += json.dumps(metadata).encode("utf-8") + _CRLF
    # Part 2: the media bytes.
    body += delim + _CRLF
    body += b"Content-Type: " + content_type.encode("ascii", "replace") + _CRLF
    if disposition:
        body += b"Content-Disposition: " + disposition.encode("ascii", "replace") + _CRLF
    body += _CRLF
    body += data + _CRLF
    # Closing delimiter.
    body += delim + b"--" + _CRLF
    return boundary, bytes(body)


def parse_boundary(content_type_header: str) -> str | None:
    """Extract the ``boundary`` value from a ``multipart/...`` Content-Type header."""
    base, _, params = content_type_header.partition(";")
    if not base.strip().lower().startswith("multipart/"):
        return None
    for param in params.split(";"):
        name, sep, value = param.strip().partition("=")
        if sep and name.strip().lower() == "boundary":
            return value.strip().strip('"')
    return None


def parse_multipart(
    content_type_header: str, body: bytes
) -> list[tuple[dict[str, str], bytes]]:
    """Parse a ``multipart/mixed`` body into ``[(headers, part_body), ...]``.

    ``headers`` keys are lower-cased. Returns ``[]`` if the body is not a parseable
    multipart (no boundary, or no complete parts) — the caller treats that as a
    failed fetch.
    """
    boundary = parse_boundary(content_type_header)
    if not boundary:
        return []
    delim = b"--" + boundary.encode("ascii")

    parts: list[tuple[dict[str, str], bytes]] = []
    # Segments between delimiters; the preamble (before the first) and the epilogue
    # (after the closing "--boundary--") are discarded.
    for segment in body.split(delim):
        if not segment or segment.startswith(b"--"):
            continue  # preamble/empty or the closing delimiter's trailing "--"
        # A well-formed segment is CRLF-wrapped: strip the leading and trailing CRLF.
        if segment.startswith(_CRLF):
            segment = segment[len(_CRLF) :]
        if segment.endswith(_CRLF):
            segment = segment[: -len(_CRLF)]
        header_blob, sep, part_body = segment.partition(_CRLF + _CRLF)
        if not sep:
            continue  # malformed part (no header/body separator)
        headers: dict[str, str] = {}
        for line in header_blob.split(_CRLF):
            name, hsep, value = line.partition(b":")
            if hsep:
                headers[name.strip().lower().decode("latin-1")] = (
                    value.strip().decode("latin-1")
                )
        parts.append((headers, part_body))
    return parts
