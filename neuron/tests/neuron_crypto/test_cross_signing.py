"""Offline tests for cross-signing (real Ed25519 signatures verified with libolm)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import olm

from neuron_crypto.cross_signing import CrossSigning
from neuron_crypto.olm_device import OlmDevice
from neuron_crypto.signing import canonical_json

USER = "@bot:hs"


def _public(key_obj: dict[str, Any]) -> str:
    return next(iter(key_obj["keys"].values()))


def test_master_signs_the_subkeys() -> None:
    body = CrossSigning(USER).device_signing_upload()
    master_pub = _public(body["master_key"])
    for sub in ("self_signing_key", "user_signing_key"):
        obj = body[sub]
        signature = obj["signatures"][USER][f"ed25519:{master_pub}"]
        # Raises olm.OlmVerifyError if the signature is invalid.
        olm.ed25519_verify(master_pub, canonical_json(obj), signature)


def test_self_signing_key_signs_a_device() -> None:
    cs = CrossSigning(USER)
    self_pub = _public(cs.device_signing_upload()["self_signing_key"])
    device_keys = OlmDevice(USER, "DEV").device_keys()

    signed = cs.sign_device(device_keys)[USER]["DEV"]
    signature = signed["signatures"][USER][f"ed25519:{self_pub}"]
    olm.ed25519_verify(self_pub, canonical_json(signed), signature)
    # The device's own self-signature is preserved alongside the cross-signature.
    assert "ed25519:DEV" in signed["signatures"][USER]


def test_persistence_roundtrip(tmp_path: Path) -> None:
    cs = CrossSigning(USER)
    master_pub = _public(cs.device_signing_upload()["master_key"])
    cs.save(str(tmp_path / "cs.json"))

    reloaded = CrossSigning.load(str(tmp_path / "cs.json"), USER)
    assert reloaded is not None
    assert _public(reloaded.device_signing_upload()["master_key"]) == master_pub
