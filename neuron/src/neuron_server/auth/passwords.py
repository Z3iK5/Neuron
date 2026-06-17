# SPDX-License-Identifier: Apache-2.0
"""Password hashing for local accounts.

Uses **PBKDF2-HMAC-SHA256** from the standard library (no extra dependency), with
a per-password random salt. Hashes are stored in a self-describing string so the
parameters travel with the hash::

    pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>

Verification is constant-time. (bcrypt/argon2 are stronger choices and are an
easy future swap — the stored format records its own algorithm so old hashes keep
verifying after a change.)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
# OWASP-aligned work factor for PBKDF2-HMAC-SHA256. Tunable; recorded per-hash.
_ITERATIONS = 210_000
_SALT_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    """Return a self-describing PBKDF2 hash of ``password``."""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str) -> bool:
    """Return True if ``password`` matches the ``stored`` hash. Never raises."""
    try:
        algorithm, iterations_s, salt_b64, hash_b64 = stored.split("$")
        iterations = int(iterations_s)
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
    except (ValueError, TypeError):
        return False
    if algorithm != _ALGORITHM:
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)
