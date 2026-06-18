# SPDX-License-Identifier: Apache-2.0
"""Passkey (WebAuthn) login for the admin console.

The console authenticates an operator with a password, then a signed session
cookie. This module adds **passkeys** as an alternative second factor / login:
the operator enrols a platform or roaming authenticator and can then sign in
with it instead of typing the password.

The heavy lifting (attestation/assertion verification) is done by ``py_webauthn``.
Registered credentials are kept in a small JSON file (the console has no database),
so this is a single-operator convenience, not a multi-user identity store.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

_RP_NAME = "NEURON Console"
# A stable handle for "the operator" (the console has a single login identity).
_USER_NAME = "operator"


@dataclass(frozen=True)
class StoredCredential:
    """A registered passkey, as persisted in the JSON store."""

    id: str  # credential id, base64url
    public_key: str  # COSE public key, base64url
    sign_count: int
    label: str
    created_ts: int


class PasskeyStore:
    """A JSON-file list of the operator's registered passkeys."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> list[dict[str, object]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, rows: list[dict[str, object]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")

    def list(self) -> list[StoredCredential]:
        return [StoredCredential(**row) for row in self._read()]  # type: ignore[arg-type]

    def has_any(self) -> bool:
        return bool(self._read())

    def get(self, credential_id: str) -> StoredCredential | None:
        return next((c for c in self.list() if c.id == credential_id), None)

    def add(self, credential: StoredCredential) -> None:
        rows = [r for r in self._read() if r.get("id") != credential.id]
        rows.append(asdict(credential))
        self._write(rows)

    def remove(self, credential_id: str) -> None:
        self._write([r for r in self._read() if r.get("id") != credential_id])

    def update_sign_count(self, credential_id: str, sign_count: int) -> None:
        rows = self._read()
        for row in rows:
            if row.get("id") == credential_id:
                row["sign_count"] = sign_count
        self._write(rows)


# --- WebAuthn ceremonies (thin wrappers over py_webauthn) -------------------


def registration_options(rp_id: str, store: PasskeyStore) -> tuple[str, str]:
    """Begin enrolment: return (options JSON for the browser, challenge base64url)."""
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=_RP_NAME,
        user_name=_USER_NAME,
        user_display_name="Console operator",
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.id)) for c in store.list()
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    return webauthn.options_to_json(options), bytes_to_base64url(options.challenge)


def verify_registration(
    credential: str, expected_challenge: str, rp_id: str, origin: str, *, label: str
) -> StoredCredential:
    """Finish enrolment: verify the attestation and return the credential to store."""
    result = webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(expected_challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
    )
    return StoredCredential(
        id=bytes_to_base64url(result.credential_id),
        public_key=bytes_to_base64url(result.credential_public_key),
        sign_count=result.sign_count,
        label=label or "passkey",
        created_ts=int(time.time() * 1000),
    )


def authentication_options(rp_id: str, store: PasskeyStore) -> tuple[str, str]:
    """Begin login: return (options JSON for the browser, challenge base64url)."""
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.id)) for c in store.list()
        ],
    )
    return webauthn.options_to_json(options), bytes_to_base64url(options.challenge)


def verify_authentication(
    credential: str, expected_challenge: str, rp_id: str, origin: str, store: PasskeyStore
) -> str:
    """Finish login: verify the assertion against a stored credential.

    Returns the credential id on success; raises if the credential is unknown or
    the assertion is invalid (``py_webauthn`` raises on failure).
    """
    credential_id = json.loads(credential)["id"]
    stored = store.get(credential_id)
    if stored is None:
        raise ValueError("Unknown passkey")
    result = webauthn.verify_authentication_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(expected_challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=base64url_to_bytes(stored.public_key),
        credential_current_sign_count=stored.sign_count,
    )
    store.update_sign_count(credential_id, result.new_sign_count)
    return credential_id
