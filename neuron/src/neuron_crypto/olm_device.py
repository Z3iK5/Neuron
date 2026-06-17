"""The bot's Olm device identity, and Olm (to-device) decryption.

To receive Megolm room keys automatically, a bot needs an **Olm device**: a
long-lived identity key pair plus a supply of one-time keys (OTKs) published to
the homeserver. A sending client claims one of those OTKs, establishes an Olm
session, and sends the bot an Olm-encrypted to-device ``m.room_key`` carrying a
Megolm session key. This class manages that device and decrypts those to-device
messages.

Requires libolm via the ``e2e`` extra.

What still needs a live homeserver (not done here): publishing the device keys /
OTKs (``/keys/upload``), a real client *choosing* to share keys (which typically
requires the bot's device to be **verified / cross-signed**), and replenishing
OTKs as they are consumed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import olm

from neuron_crypto.signing import canonical_json as _canonical_json

OLM_ALGORITHM = "m.olm.v1.curve25519-aes-sha2"
MEGOLM_ALGORITHM = "m.megolm.v1.aes-sha2"


class OlmDevice:
    """Wraps an ``olm.Account`` (the device identity) + its Olm sessions."""

    def __init__(
        self,
        user_id: str,
        device_id: str,
        *,
        account: olm.Account | None = None,
        pickle_key: str = "neuron",
    ) -> None:
        self.user_id = user_id
        self.device_id = device_id
        self.account = account or olm.Account()
        self._pickle_key = pickle_key
        # Olm sessions keyed by the other device's curve25519 identity key.
        self._sessions: dict[str, list[olm.Session]] = {}

    @property
    def curve25519(self) -> str:
        return self.account.identity_keys["curve25519"]

    @property
    def ed25519(self) -> str:
        return self.account.identity_keys["ed25519"]

    # --- signing & key publication -----------------------------------------
    def _sign(self, obj: dict[str, Any]) -> str:
        signature: str = self.account.sign(_canonical_json(obj))
        return signature

    def device_keys(self) -> dict[str, Any]:
        """The signed ``device_keys`` object for ``/keys/upload``."""
        keys: dict[str, Any] = {
            "user_id": self.user_id,
            "device_id": self.device_id,
            "algorithms": [OLM_ALGORITHM, MEGOLM_ALGORITHM],
            "keys": {
                f"curve25519:{self.device_id}": self.curve25519,
                f"ed25519:{self.device_id}": self.ed25519,
            },
        }
        keys["signatures"] = {self.user_id: {f"ed25519:{self.device_id}": self._sign(keys)}}
        return keys

    def generate_one_time_keys(self, count: int) -> dict[str, Any]:
        """Generate ``count`` OTKs and return the signed ``one_time_keys`` upload map.

        Marks the keys as published (call only when you're about to upload them).
        """
        self.account.generate_one_time_keys(count)
        signed: dict[str, Any] = {}
        for key_id, key in self.account.one_time_keys["curve25519"].items():
            obj: dict[str, Any] = {"key": key}
            obj["signatures"] = {self.user_id: {f"ed25519:{self.device_id}": self._sign(obj)}}
            signed[f"signed_curve25519:{key_id}"] = obj
        self.account.mark_keys_as_published()
        return signed

    # --- to-device Olm decryption ------------------------------------------
    @staticmethod
    def _message(message_type: int, body: str) -> olm.OlmMessage | olm.OlmPreKeyMessage:
        return olm.OlmPreKeyMessage(body) if message_type == 0 else olm.OlmMessage(body)

    def decrypt_to_device(
        self, sender_key: str, message_type: int, body: str
    ) -> dict[str, Any] | None:
        """Decrypt one Olm to-device message from ``sender_key``. Returns the payload.

        ``message_type`` 0 = pre-key (starts a session), 1 = normal. Returns the
        decrypted JSON payload (e.g. an ``m.room_key``), or ``None`` if it can't be
        decrypted.
        """
        sessions = self._sessions.setdefault(sender_key, [])

        # Try existing sessions first (a fresh message wrapper per attempt).
        for session in sessions:
            try:
                return self._decode(session.decrypt(self._message(message_type, body)))
            except olm.OlmSessionError:
                continue

        # No existing session: a pre-key message starts a new inbound session.
        if message_type == 0:
            new_session = olm.InboundSession(self.account, olm.OlmPreKeyMessage(body), sender_key)
            self.account.remove_one_time_keys(new_session)
            payload = self._decode(new_session.decrypt(olm.OlmPreKeyMessage(body)))
            sessions.append(new_session)
            return payload
        return None

    @staticmethod
    def _decode(plaintext: str) -> dict[str, Any] | None:
        try:
            data = json.loads(plaintext)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    # --- persistence --------------------------------------------------------
    def save(self, path: str) -> None:
        """Persist the account + Olm sessions so the device survives a restart."""
        data = {
            "user_id": self.user_id,
            "device_id": self.device_id,
            "account": base64.b64encode(self.account.pickle(self._pickle_key)).decode("ascii"),
            "sessions": {
                sender: [base64.b64encode(s.pickle(self._pickle_key)).decode("ascii") for s in sess]
                for sender, sess in self._sessions.items()
            },
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data))

    @classmethod
    def load(
        cls, path: str, user_id: str, device_id: str, *, pickle_key: str = "neuron"
    ) -> OlmDevice | None:
        """Load a previously saved device, or ``None`` if the file doesn't exist."""
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        account = olm.Account.from_pickle(base64.b64decode(data["account"]), pickle_key)
        device = cls(
            data.get("user_id", user_id),
            data.get("device_id", device_id),
            account=account,
            pickle_key=pickle_key,
        )
        for sender, pickled_list in data.get("sessions", {}).items():
            device._sessions[sender] = [
                olm.Session.from_pickle(base64.b64decode(b64), pickle_key) for b64 in pickled_list
            ]
        return device
