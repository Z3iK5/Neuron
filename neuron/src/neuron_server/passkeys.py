# SPDX-License-Identifier: Apache-2.0
"""WebAuthn passkey ceremonies for console sign-in.

Thin wrappers over ``py_webauthn`` that operate on plain values, so the console
routes can persist credentials in the database (keyed by the owning admin account).
Imported lazily by the passkey routes so ``webauthn`` is only required when the
feature is actually used.
"""

from __future__ import annotations

import json
import time
from typing import Any

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

_RP_NAME = "NEURON"


def registration_options(rp_id: str, *, user_id: str, exclude_ids: list[str]) -> tuple[str, str]:
    """Begin enrolment: return (options JSON for the browser, challenge base64url)."""
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=_RP_NAME,
        user_name=user_id,
        user_display_name=user_id,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid)) for cid in exclude_ids
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    return webauthn.options_to_json(options), bytes_to_base64url(options.challenge)


def verify_registration(
    credential: str, expected_challenge: str, rp_id: str, origin: str, *, label: str
) -> dict[str, Any]:
    """Finish enrolment: verify the attestation; return the credential row to store."""
    result = webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(expected_challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
    )
    return {
        "credential_id": bytes_to_base64url(result.credential_id),
        "public_key": bytes_to_base64url(result.credential_public_key),
        "sign_count": result.sign_count,
        "label": label or "passkey",
        "created_ts": int(time.time() * 1000),
    }


def authentication_options(rp_id: str, *, allow_ids: list[str]) -> tuple[str, str]:
    """Begin login: return (options JSON for the browser, challenge base64url)."""
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid)) for cid in allow_ids
        ],
    )
    return webauthn.options_to_json(options), bytes_to_base64url(options.challenge)


def credential_id_of(credential: str) -> str:
    """The credential id the browser is asserting (to look up the stored key)."""
    return str(json.loads(credential)["id"])


def verify_authentication(
    credential: str,
    expected_challenge: str,
    rp_id: str,
    origin: str,
    *,
    public_key: str,
    sign_count: int,
) -> int:
    """Finish login: verify the assertion; return the new sign count (raises on failure)."""
    result = webauthn.verify_authentication_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(expected_challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=base64url_to_bytes(public_key),
        credential_current_sign_count=sign_count,
    )
    return int(result.new_sign_count)
