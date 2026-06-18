# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the X-Matrix federation request authentication scheme."""

from __future__ import annotations

from neuron_server.crypto.signing import generate_signing_key
from neuron_server.federation.auth import (
    parse_authorization_header,
    sign_request,
    verify_request,
)


def _verify_keys(key: object) -> dict[str, str]:
    return {key.key_id: key.verify_key_base64()}  # type: ignore[attr-defined]


def test_sign_then_parse_then_verify_get() -> None:
    key = generate_signing_key("a_k")
    header = sign_request(
        method="GET",
        uri="/_matrix/federation/v1/state/!r:hs.b",
        origin="hs.a",
        destination="hs.b",
        signing_key=key,
    )
    creds = parse_authorization_header(header)
    assert creds is not None
    assert creds.origin == "hs.a" and creds.destination == "hs.b" and creds.key_id == key.key_id
    assert verify_request(
        creds,
        method="GET",
        uri="/_matrix/federation/v1/state/!r:hs.b",
        destination="hs.b",
        verify_keys=_verify_keys(key),
    )


def test_verify_with_content_roundtrip() -> None:
    key = generate_signing_key("a_k")
    body = {"pdus": [{"type": "m.room.message"}]}
    header = sign_request(
        method="PUT",
        uri="/_matrix/federation/v1/send/txn1",
        origin="hs.a",
        destination="hs.b",
        signing_key=key,
        content=body,
    )
    creds = parse_authorization_header(header)
    assert creds is not None
    assert verify_request(
        creds,
        method="PUT",
        uri="/_matrix/federation/v1/send/txn1",
        destination="hs.b",
        verify_keys=_verify_keys(key),
        content=body,
    )


def test_verify_fails_on_tampered_uri_or_key() -> None:
    key = generate_signing_key("a_k")
    header = sign_request(
        method="GET", uri="/a", origin="hs.a", destination="hs.b", signing_key=key
    )
    creds = parse_authorization_header(header)
    assert creds is not None
    # A different URI does not verify.
    assert not verify_request(
        creds, method="GET", uri="/b", destination="hs.b", verify_keys=_verify_keys(key)
    )
    # An unknown key id does not verify.
    other = generate_signing_key("a_other")
    assert not verify_request(
        creds, method="GET", uri="/a", destination="hs.b", verify_keys=_verify_keys(other)
    )


def test_parse_rejects_non_xmatrix() -> None:
    assert parse_authorization_header("Bearer abc") is None
    assert parse_authorization_header("") is None
    assert parse_authorization_header("X-Matrix origin=\"hs\"") is None  # missing key/sig
