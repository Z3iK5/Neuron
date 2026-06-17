# SPDX-License-Identifier: Apache-2.0
"""E2EEManager — receive Megolm keys via to-device, then decrypt room events.

This ties together the halves of automatic E2EE:

- ``handle_to_device(events)`` — decrypts Olm-encrypted to-device messages with the
  bot's :class:`OlmDevice`; when one is an ``m.room_key``, the Megolm session it
  carries is imported into the :class:`MegolmSessionStore`.
- ``decrypt(event)`` — decrypts an ``m.room.encrypted`` room event against the keys
  collected so far (the ``Decryptor`` interface the auditor uses).
- ``maybe_generate_one_time_keys(count)`` — tops up the bot's published one-time
  keys when the server reports they're running low (so senders can keep
  establishing Olm sessions to share keys).

When ``device_path`` / ``store_path`` are given, the manager persists the evolving
crypto state (Olm sessions, received room keys) so a restart keeps it.
"""

from __future__ import annotations

from typing import Any

from neuron_crypto.base import DecryptResult
from neuron_crypto.megolm import MegolmSessionStore
from neuron_crypto.olm_device import OLM_ALGORITHM, OlmDevice


class E2EEManager:
    """A ``Decryptor`` that also ingests room keys and manages one-time keys."""

    def __init__(
        self,
        device: OlmDevice,
        store: MegolmSessionStore | None = None,
        *,
        device_path: str | None = None,
        store_path: str | None = None,
        otk_target: int = 50,
        otk_minimum: int = 20,
    ) -> None:
        self.device = device
        self.store = store or MegolmSessionStore()
        self._device_path = device_path
        self._store_path = store_path
        self._otk_target = otk_target
        self._otk_minimum = otk_minimum

    def handle_to_device(self, events: list[dict[str, Any]]) -> int:
        """Process to-device events, importing any Megolm keys. Returns count imported."""
        imported = 0
        touched_device = False
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
            touched_device = True  # decrypting may have created an Olm session
            if payload and payload.get("type") == "m.room_key":
                if self.store.import_room_key(payload.get("content", {})) is not None:
                    imported += 1
        if touched_device:
            self._persist_device()
        if imported:
            self._persist_store()
        return imported

    def decrypt(self, event: dict[str, Any]) -> DecryptResult:
        return self.store.decrypt_event(event)

    def maybe_generate_one_time_keys(self, current_count: int) -> dict[str, Any] | None:
        """If published one-time keys are low, generate more. Returns the upload map.

        Returns ``None`` when there are still enough keys. Callers should
        ``keys_upload`` the returned map.
        """
        if current_count >= self._otk_minimum:
            return None
        needed = max(self._otk_target - current_count, 1)
        keys = self.device.generate_one_time_keys(needed)
        self._persist_device()
        return keys

    def _persist_device(self) -> None:
        if self._device_path:
            self.device.save(self._device_path)

    def _persist_store(self) -> None:
        if self._store_path:
            self.store.save(self._store_path)
