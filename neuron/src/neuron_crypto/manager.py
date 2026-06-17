"""E2EEManager — receive Megolm keys via to-device, then decrypt room events.

This ties together the two halves of automatic E2EE:

- ``handle_to_device(events)`` — decrypts Olm-encrypted to-device messages with the
  bot's :class:`OlmDevice`; when one is an ``m.room_key``, the Megolm session it
  carries is imported into the :class:`MegolmSessionStore`.
- ``decrypt(event)`` — decrypts an ``m.room.encrypted`` room event against the keys
  collected so far (the ``Decryptor`` interface the auditor uses).

So once the bot has been sent a room's key, subsequent messages in that room
decrypt automatically — no manual key file needed.
"""

from __future__ import annotations

from typing import Any

from neuron_crypto.base import DecryptResult
from neuron_crypto.megolm import MegolmSessionStore
from neuron_crypto.olm_device import OLM_ALGORITHM, OlmDevice


class E2EEManager:
    """A ``Decryptor`` that also ingests room keys from to-device messages."""

    def __init__(self, device: OlmDevice, store: MegolmSessionStore | None = None) -> None:
        self.device = device
        self.store = store or MegolmSessionStore()

    def handle_to_device(self, events: list[dict[str, Any]]) -> int:
        """Process to-device events, importing any Megolm keys. Returns count imported."""
        imported = 0
        for event in events:
            if event.get("type") != "m.room.encrypted":
                continue
            content = event.get("content", {}) or {}
            if content.get("algorithm") != OLM_ALGORITHM:
                continue
            sender_key = content.get("sender_key")
            ours = (content.get("ciphertext") or {}).get(self.device.curve25519)
            if not isinstance(sender_key, str) or not isinstance(ours, dict):
                continue
            payload = self.device.decrypt_to_device(
                sender_key, ours.get("type", 0), ours.get("body", "")
            )
            if payload and payload.get("type") == "m.room_key":
                if self.store.import_room_key(payload.get("content", {})) is not None:
                    imported += 1
        return imported

    def decrypt(self, event: dict[str, Any]) -> DecryptResult:
        return self.store.decrypt_event(event)
