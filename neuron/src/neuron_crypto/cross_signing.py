# SPDX-License-Identifier: Apache-2.0
"""Cross-signing keys for the bot's verifiable identity.

Matrix "cross-signing" gives a user three Ed25519 keys:

- **master** — the root of the user's identity;
- **self-signing** — signs the user's own devices (so other users can trust a
  device by trusting the master key once);
- **user-signing** — signs *other* users' master keys.

A bot that publishes these and self-signs its device presents a proper,
verifiable identity. Whether other users' clients then *share room keys* with the
bot is their trust decision (verifying the bot's master key, or a policy that
shares with cross-signed devices) — that part is operational/live.

Requires libolm via the ``e2e`` extra.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import olm

from neuron_crypto.signing import canonical_json

_NAMES = ("master", "self_signing", "user_signing")


class CrossSigning:
    """Holds a user's three cross-signing keys and builds signed upload payloads."""

    def __init__(self, user_id: str, seeds: dict[str, bytes] | None = None) -> None:
        self.user_id = user_id
        self._seeds = seeds or {name: olm.PkSigning.generate_seed() for name in _NAMES}
        self._keys = {name: olm.PkSigning(seed) for name, seed in self._seeds.items()}

    def _public(self, name: str) -> str:
        public_key: str = self._keys[name].public_key
        return public_key

    def _key_object(self, name: str) -> dict[str, Any]:
        pub = self._public(name)
        return {
            "user_id": self.user_id,
            "usage": [name],
            "keys": {f"ed25519:{pub}": pub},
        }

    def device_signing_upload(self) -> dict[str, Any]:
        """Build the body for ``POST /_matrix/client/v3/keys/device_signing/upload``.

        The master key signs the self-signing and user-signing keys.
        """
        master_pub = self._public("master")
        master = self._key_object("master")
        bodies = {"master_key": master}
        for name in ("self_signing", "user_signing"):
            obj = self._key_object(name)
            signature = self._keys["master"].sign(canonical_json(obj))
            obj["signatures"] = {self.user_id: {f"ed25519:{master_pub}": signature}}
            bodies[f"{name}_key"] = obj
        return bodies

    def sign_device(self, device_keys: dict[str, Any]) -> dict[str, Any]:
        """Sign a device's keys with the self-signing key.

        Returns the body for ``POST /_matrix/client/v3/keys/signatures/upload``:
        ``{user_id: {device_id: <device_keys with the added signature>}}``.
        """
        self_pub = self._public("self_signing")
        signature = self._keys["self_signing"].sign(canonical_json(device_keys))
        signed = json.loads(json.dumps(device_keys))  # deep copy
        signed.setdefault("signatures", {}).setdefault(self.user_id, {})[
            f"ed25519:{self_pub}"
        ] = signature
        return {self.user_id: {device_keys["device_id"]: signed}}

    # --- persistence --------------------------------------------------------
    def save(self, path: str) -> None:
        """Persist the cross-signing seeds (highly sensitive — protect this file)."""
        data = {name: base64.b64encode(seed).decode("ascii") for name, seed in self._seeds.items()}
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"user_id": self.user_id, "seeds": data}))

    @classmethod
    def load(cls, path: str, user_id: str) -> CrossSigning | None:
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        seeds = {name: base64.b64decode(b64) for name, b64 in data["seeds"].items()}
        return cls(data.get("user_id", user_id), seeds)
