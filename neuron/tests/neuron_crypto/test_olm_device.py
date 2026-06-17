"""Offline tests for the automatic E2EE key-receipt pipeline (real libolm).

Simulates a *sending* device that claims the bot's one-time key, opens an Olm
session, and sends an Olm-encrypted ``m.room_key`` to-device message carrying a
Megolm session. The bot's E2EEManager must ingest that key and then decrypt a
Megolm room message — all without a server.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import olm

from neuron_crypto.manager import E2EEManager
from neuron_crypto.megolm import MEGOLM_ALGORITHM
from neuron_crypto.olm_device import OLM_ALGORITHM, OlmDevice


def _olm_to_device_event(sender: olm.Account, bot_curve: str, otk: str, payload: dict[str, Any]):
    session = olm.OutboundSession(sender, bot_curve, otk)
    msg = session.encrypt(json.dumps(payload))
    return {
        "type": "m.room.encrypted",
        "sender": "@sender:hs",
        "content": {
            "algorithm": OLM_ALGORITHM,
            "sender_key": sender.identity_keys["curve25519"],
            "ciphertext": {bot_curve: {"type": msg.message_type, "body": msg.ciphertext}},
        },
    }


def _megolm_room_event(group: olm.OutboundGroupSession, body: str) -> dict[str, Any]:
    inner = json.dumps({"type": "m.room.message", "content": {"body": body}, "room_id": "!r:hs"})
    return {
        "type": "m.room.encrypted",
        "event_id": "$msg",
        "sender": "@sender:hs",
        "content": {
            "algorithm": MEGOLM_ALGORITHM,
            "sender_key": "SENDERCURVE",
            "session_id": group.id,
            "ciphertext": group.encrypt(inner),
        },
    }


def test_full_automatic_pipeline() -> None:
    # Bot device publishes a one-time key the sender can claim.
    bot = OlmDevice("@bot:hs", "BOTDEV")
    bot.account.generate_one_time_keys(1)
    otk = next(iter(bot.account.one_time_keys["curve25519"].values()))
    bot.account.mark_keys_as_published()

    # The sender creates a Megolm room session and shares it via an Olm to-device key.
    sender = olm.Account()
    group = olm.OutboundGroupSession()
    room_key_payload = {
        "type": "m.room_key",
        "content": {
            "algorithm": MEGOLM_ALGORITHM,
            "room_id": "!r:hs",
            "session_id": group.id,
            "session_key": group.session_key,
        },
    }
    to_device = _olm_to_device_event(sender, bot.curve25519, otk, room_key_payload)

    manager = E2EEManager(bot)
    # Before the key arrives, the room message cannot be decrypted.
    assert manager.decrypt(_megolm_room_event(group, "hi")).decrypted is False

    # The bot ingests the to-device key...
    assert manager.handle_to_device([to_device]) == 1

    # ...and now decrypts room messages in that session automatically.
    result = manager.decrypt(_megolm_room_event(group, "now visible"))
    assert result.decrypted is True
    assert result.content == {"body": "now visible"}


def test_device_keys_are_signed() -> None:
    bot = OlmDevice("@bot:hs", "BOTDEV")
    keys = bot.device_keys()
    assert keys["user_id"] == "@bot:hs"
    assert keys["keys"]["curve25519:BOTDEV"] == bot.curve25519
    assert "ed25519:BOTDEV" in keys["signatures"]["@bot:hs"]


def test_one_time_keys_upload_shape() -> None:
    bot = OlmDevice("@bot:hs", "BOTDEV")
    otks = bot.generate_one_time_keys(2)
    assert len(otks) == 2
    assert all(k.startswith("signed_curve25519:") for k in otks)
    assert all("signatures" in v for v in otks.values())


def test_device_persistence(tmp_path: Path) -> None:
    bot = OlmDevice("@bot:hs", "BOTDEV")
    curve = bot.curve25519
    path = str(tmp_path / "device.json")
    bot.save(path)

    reloaded = OlmDevice.load(path, "@bot:hs", "BOTDEV")
    assert reloaded is not None
    assert reloaded.curve25519 == curve


def test_one_time_key_replenishment() -> None:
    manager = E2EEManager(OlmDevice("@bot:hs", "BOTDEV"), otk_target=10, otk_minimum=5)
    # Plenty of keys server-side -> nothing to do.
    assert manager.maybe_generate_one_time_keys(8) is None
    # Running low -> generate up to the target (10 - 2 = 8 new keys).
    keys = manager.maybe_generate_one_time_keys(2)
    assert keys is not None
    assert len(keys) == 8
    assert all(k.startswith("signed_curve25519:") for k in keys)
