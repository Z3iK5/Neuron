# SPDX-License-Identifier: Apache-2.0
"""Offline tests for Megolm decryption (real libolm round-trips).

These create a real outbound Megolm session (the "sender"), encrypt an event,
import the matching inbound session key into our store, and decrypt — proving the
decryption path works end to end without any server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import olm

from neuron_crypto.megolm import MEGOLM_ALGORITHM, MegolmDecryptor, MegolmSessionStore


def _encrypt_event(outbound: olm.OutboundGroupSession, room_id: str, body: str) -> dict[str, Any]:
    payload = json.dumps(
        {
            "type": "m.room.message",
            "content": {"body": body, "msgtype": "m.text"},
            "room_id": room_id,
        }
    )
    return {
        "type": "m.room.encrypted",
        "event_id": "$enc",
        "sender": "@alice:hs",
        "content": {
            "algorithm": MEGOLM_ALGORITHM,
            "sender_key": "SENDER_CURVE25519",
            "session_id": outbound.id,
            "ciphertext": outbound.encrypt(payload),
            "device_id": "DEV1",
        },
    }


def test_roundtrip_decrypts_inner_event() -> None:
    outbound = olm.OutboundGroupSession()
    store = MegolmSessionStore()
    session_id = store.import_session_key(outbound.session_key)
    assert session_id == outbound.id

    result = store.decrypt_event(_encrypt_event(outbound, "!r:hs", "top secret"))
    assert result.decrypted is True
    assert result.event_type == "m.room.message"
    assert result.content == {"body": "top secret", "msgtype": "m.text"}
    assert result.sender_curve25519 == "SENDER_CURVE25519"


def test_missing_key_reports_no_session() -> None:
    outbound = olm.OutboundGroupSession()
    event = _encrypt_event(outbound, "!r:hs", "secret")
    result = MegolmSessionStore().decrypt_event(event)  # no key imported
    assert result.decrypted is False
    assert result.reason is not None and "no session" in result.reason


def test_non_megolm_event_is_not_decrypted() -> None:
    event = {"type": "m.room.message", "content": {"body": "plain"}}
    result = MegolmSessionStore().decrypt_event(event)
    assert result.decrypted is False


def test_persistence_roundtrip(tmp_path: Path) -> None:
    outbound = olm.OutboundGroupSession()
    store = MegolmSessionStore()
    store.import_session_key(outbound.session_key)
    store.save(str(tmp_path / "keys.json"))

    reloaded = MegolmSessionStore()
    assert reloaded.load(str(tmp_path / "keys.json")) == 1
    assert reloaded.decrypt_event(_encrypt_event(outbound, "!r:hs", "after restart")).decrypted


def test_import_key_file(tmp_path: Path) -> None:
    outbound = olm.OutboundGroupSession()
    keyfile = tmp_path / "export.json"
    keyfile.write_text(
        json.dumps([{"algorithm": MEGOLM_ALGORITHM, "session_key": outbound.session_key}])
    )
    store = MegolmSessionStore()
    assert store.import_key_file(str(keyfile)) == 1

    decryptor = MegolmDecryptor(store)
    assert decryptor.decrypt(_encrypt_event(outbound, "!r:hs", "hi")).decrypted is True
