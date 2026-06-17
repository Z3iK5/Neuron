# SPDX-License-Identifier: Apache-2.0
"""Matrix identifier helpers: user-ID localparts, device IDs, access tokens.

The Client-Server API constrains a user-ID localpart to the characters
``a-z 0-9 . _ = - / +`` and requires the full ``@localpart:server_name`` to be at
most 255 bytes. Device IDs and access tokens are server-chosen opaque strings.
"""

from __future__ import annotations

import re
import secrets
import string

# Allowed characters in a (historical-grammar) localpart. We require lowercase.
_LOCALPART_RE = re.compile(r"^[a-z0-9._=/+\-]+$")

_MAX_USER_ID_LENGTH = 255
_DEVICE_ID_ALPHABET = string.ascii_uppercase
_DEVICE_ID_LENGTH = 10
_GENERATED_LOCALPART_BYTES = 8


def is_valid_localpart(localpart: str) -> bool:
    """Return True if ``localpart`` is a syntactically valid user-ID localpart."""
    return bool(localpart) and _LOCALPART_RE.match(localpart) is not None


def is_valid_user_id(user_id: str) -> bool:
    """Return True if ``user_id`` (``@localpart:server_name``) is within length limits."""
    return 0 < len(user_id) <= _MAX_USER_ID_LENGTH


def generate_device_id() -> str:
    """Return a fresh random device ID (10 uppercase letters)."""
    return "".join(secrets.choice(_DEVICE_ID_ALPHABET) for _ in range(_DEVICE_ID_LENGTH))


def generate_access_token() -> str:
    """Return a fresh, opaque, URL-safe access token."""
    return secrets.token_urlsafe(32)


def generate_localpart() -> str:
    """Return a random lowercase localpart (used when a client omits ``username``)."""
    return secrets.token_hex(_GENERATED_LOCALPART_BYTES)
