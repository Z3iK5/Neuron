# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Ed25519 signing primitives (HS-7)."""

from __future__ import annotations

from nacl.signing import SigningKey as NaclSigningKey

from neuron_server.crypto.signing import (
    SigningKey,
    canonical_json,
    decode_unpadded_base64,
    encode_unpadded_base64,
    generate_signing_key,
    parse_signing_key,
    sign_json,
    verify_signed_json,
)


def test_unpadded_base64_roundtrip() -> None:
    for raw in (b"", b"a", b"ab", b"abc", bytes(range(32))):
        encoded = encode_unpadded_base64(raw)
        assert "=" not in encoded
        assert decode_unpadded_base64(encoded) == raw


def test_canonical_json_sorts_keys_without_whitespace() -> None:
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    # Nested objects are canonicalised recursively.
    assert canonical_json({"z": {"y": 1, "x": 2}}) == b'{"z":{"x":2,"y":1}}'


def test_signing_key_serialize_parse_roundtrip() -> None:
    key = generate_signing_key("a_test1")
    restored = parse_signing_key(key.serialize())
    assert restored.version == key.version
    assert restored.key_id == "ed25519:a_test1"
    assert restored.seed == key.seed
    assert restored.verify_key_base64() == key.verify_key_base64()


def test_signature_matches_libsodium_reference() -> None:
    # Cross-check our wrapper against the underlying libsodium primitives: the
    # Ed25519 maths is delegated, so identical output proves we wire it correctly.
    seed = bytes(range(32))
    nacl_key = NaclSigningKey(seed)
    ours = SigningKey(version="1", _signing=nacl_key)
    message = b"matrix federation"
    assert ours.sign(message) == nacl_key.sign(message).signature
    assert ours.verify_key_base64() == encode_unpadded_base64(bytes(nacl_key.verify_key))


def test_sign_json_roundtrip_and_tamper_detection() -> None:
    key = generate_signing_key("a_k")
    signed = sign_json({"a": 1, "b": [2, 3]}, server_name="hs.example", signing_key=key)
    assert verify_signed_json(
        signed,
        server_name="hs.example",
        verify_key_base64=key.verify_key_base64(),
        key_id=key.key_id,
    )
    # Tampering with a signed field invalidates the signature.
    tampered = dict(signed)
    tampered["a"] = 999
    assert not verify_signed_json(
        tampered,
        server_name="hs.example",
        verify_key_base64=key.verify_key_base64(),
        key_id=key.key_id,
    )
    # A different key does not verify.
    other = generate_signing_key("a_k")
    assert not verify_signed_json(
        signed,
        server_name="hs.example",
        verify_key_base64=other.verify_key_base64(),
        key_id=key.key_id,
    )


def test_signature_ignores_signatures_and_unsigned_members() -> None:
    key = generate_signing_key("a_k")
    signed = sign_json({"a": 1}, server_name="hs.example", signing_key=key)
    # Adding an `unsigned` member after signing must not break verification.
    signed["unsigned"] = {"age": 5}
    assert verify_signed_json(
        signed,
        server_name="hs.example",
        verify_key_base64=key.verify_key_base64(),
        key_id=key.key_id,
    )


def test_sign_json_preserves_existing_signatures() -> None:
    a = generate_signing_key("a_a")
    b = generate_signing_key("a_b")
    once = sign_json({"x": 1}, server_name="hs.a", signing_key=a)
    twice = sign_json(once, server_name="hs.b", signing_key=b)
    assert verify_signed_json(
        twice, server_name="hs.a", verify_key_base64=a.verify_key_base64(), key_id=a.key_id
    )
    assert verify_signed_json(
        twice, server_name="hs.b", verify_key_base64=b.verify_key_base64(), key_id=b.key_id
    )
