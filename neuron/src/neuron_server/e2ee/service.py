# SPDX-License-Identifier: Apache-2.0
"""End-to-end-encryption relay service (the server never decrypts).

Implements the key-distribution side of Matrix E2EE: device identity keys,
one-time keys (claimed to bootstrap Olm sessions), cross-signing keys, signature
merging, and to-device message delivery (how Olm-encrypted ``m.room_key`` events
reach a recipient). All payloads are opaque to the server.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from neuron_core import get_logger
from neuron_server.storage import accounts
from neuron_server.storage import e2ee as store
from neuron_server.storage.database import Database

_logger = get_logger(__name__)

_CROSS_SIGNING_TYPES = {
    "master_key": "master",
    "self_signing_key": "self_signing",
    "user_signing_key": "user_signing",
}

# Announces a local device-list change over federation:
# (user_id, device_id, stream_id, deleted).
FederationPush = Callable[[str, str, int, bool], Awaitable[None]]


class E2EEService:
    """Stores and relays E2EE key material for one server."""

    def __init__(
        self,
        db: Database,
        notify: Callable[[], None] | None = None,
        *,
        federation_push: FederationPush | None = None,
    ) -> None:
        self._db = db
        self._notify = notify
        self._federation_push = federation_push

    def _wake_syncs(self) -> None:
        if self._notify is not None:
            self._notify()

    async def _push_device_change(
        self, user_id: str, device_id: str, stream_id: int, deleted: bool
    ) -> None:
        """Best-effort federation announcement; never breaks the local action."""
        if self._federation_push is None:
            return
        try:
            await self._federation_push(user_id, device_id, stream_id, deleted)
        except Exception:
            _logger.warning(
                "failed to send device-list update for %s over federation", user_id
            )

    async def notify_device_change(
        self, user_id: str, device_id: str, deleted: bool = False
    ) -> None:
        """A local user's device was added/removed: bump the device-list stream
        (so local users sharing a room see ``device_lists.changed``) and announce
        it to remote servers sharing a room with the user."""
        # Allocate the stream id inside a transaction so the Postgres position
        # tracker advances the /sync floor on commit — a bare allocation moves no
        # floor, so every incremental /sync would re-report this change forever
        # (and never park a long-poll). Matches the upload_keys write path.
        async with self._db.transaction():
            stream_id = await store.bump_device_list(self._db, user_id)
        self._wake_syncs()
        await self._push_device_change(user_id, device_id, stream_id, deleted)

    # --- keys/upload -------------------------------------------------------

    async def upload_keys(
        self, user_id: str, device_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        device_keys = body.get("device_keys")
        one_time_keys = body.get("one_time_keys") or {}
        fallback_keys = body.get("fallback_keys") or body.get(
            "org.matrix.msc2732.fallback_keys"
        ) or {}

        stream_id: int | None = None
        async with self._db.transaction():
            if isinstance(device_keys, dict):
                await store.upsert_device_keys(
                    self._db, user_id, device_id, json.dumps(device_keys)
                )
                stream_id = await store.bump_device_list(self._db, user_id)
            if one_time_keys:
                await store.store_one_time_keys(self._db, user_id, device_id, one_time_keys)
            if fallback_keys:
                await store.store_fallback_keys(self._db, user_id, device_id, fallback_keys)

        if stream_id is not None:
            self._wake_syncs()
            await self._push_device_change(user_id, device_id, stream_id, False)
        counts = await store.count_one_time_keys(self._db, user_id, device_id)
        return {"one_time_key_counts": counts}

    # --- keys/query --------------------------------------------------------

    async def query_keys(self, body: dict[str, Any]) -> dict[str, Any]:
        requested = body.get("device_keys") or {}
        device_keys: dict[str, Any] = {}
        master: dict[str, Any] = {}
        self_signing: dict[str, Any] = {}
        user_signing: dict[str, Any] = {}

        for user_id, device_filter in requested.items():
            all_devices = await store.get_device_keys_for_user(self._db, user_id)
            if device_filter:
                all_devices = {d: k for d, k in all_devices.items() if d in device_filter}
            if all_devices:
                device_keys[user_id] = all_devices

            for key_type, target in (
                ("master", master),
                ("self_signing", self_signing),
                ("user_signing", user_signing),
            ):
                key = await store.get_cross_signing_key(self._db, user_id, key_type)
                if key is not None:
                    target[user_id] = key

        return {
            "device_keys": device_keys,
            "master_keys": master,
            "self_signing_keys": self_signing,
            "user_signing_keys": user_signing,
            "failures": {},
        }

    # --- keys/claim --------------------------------------------------------

    async def claim_keys(self, body: dict[str, Any]) -> dict[str, Any]:
        requested = body.get("one_time_keys") or {}
        claimed: dict[str, Any] = {}

        async with self._db.transaction():
            for user_id, devices in requested.items():
                for device_id, algorithm in devices.items():
                    key = await store.claim_one_time_key(
                        self._db, user_id, device_id, str(algorithm)
                    )
                    if key is not None:
                        claimed.setdefault(user_id, {})[device_id] = key
        return {"one_time_keys": claimed, "failures": {}}

    # --- cross-signing & signatures ---------------------------------------

    async def upload_cross_signing_keys(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        async with self._db.transaction():
            stored = False
            for field, key_type in _CROSS_SIGNING_TYPES.items():
                key = body.get(field)
                if isinstance(key, dict):
                    await store.upsert_cross_signing_key(
                        self._db, user_id, key_type, json.dumps(key)
                    )
                    stored = True
            if stored:
                await store.bump_device_list(self._db, user_id)
        if stored:
            self._wake_syncs()
        return {}

    async def upload_signatures(self, body: dict[str, Any]) -> dict[str, Any]:
        async with self._db.transaction():
            for user_id, items in body.items():
                if not isinstance(items, dict):
                    continue
                for key_id, signed in items.items():
                    if isinstance(signed, dict):
                        await self._merge_signatures(user_id, key_id, signed)
        return {"failures": {}}

    async def _merge_signatures(self, user_id: str, key_id: str, signed: dict[str, Any]) -> None:
        new_sigs = signed.get("signatures")
        if not isinstance(new_sigs, dict):
            return

        # A device signature: key_id is the device ID.
        existing = await store.get_device_keys(self._db, user_id, key_id)
        if existing is not None:
            _deep_merge_signatures(existing, new_sigs)
            await store.upsert_device_keys(self._db, user_id, key_id, json.dumps(existing))
            return

        # Otherwise it may target a cross-signing key (match by its public key id).
        for key_type in ("master", "self_signing", "user_signing"):
            key = await store.get_cross_signing_key(self._db, user_id, key_type)
            if key is not None and key_id in (key.get("keys") or {}).values():
                _deep_merge_signatures(key, new_sigs)
                await store.upsert_cross_signing_key(
                    self._db, user_id, key_type, json.dumps(key)
                )
                return

    # --- sendToDevice ------------------------------------------------------

    async def send_to_device(
        self, sender: str, event_type: str, messages: dict[str, Any]
    ) -> None:
        async with self._db.transaction():
            for target_user, by_device in messages.items():
                if not isinstance(by_device, dict):
                    continue
                for target_device, content in by_device.items():
                    if target_device == "*":
                        for device in await accounts.list_devices(self._db, target_user):
                            await store.add_to_device_message(
                                self._db, target_user, device.device_id, sender, event_type, content
                            )
                    else:
                        await store.add_to_device_message(
                            self._db, target_user, target_device, sender, event_type, content
                        )
        self._wake_syncs()


def _deep_merge_signatures(target: dict[str, Any], new_sigs: dict[str, Any]) -> None:
    """Merge ``new_sigs`` into ``target['signatures']`` (per-user, per-key)."""
    sigs = target.setdefault("signatures", {})
    for signer, keys in new_sigs.items():
        if isinstance(keys, dict):
            sigs.setdefault(signer, {}).update(keys)
