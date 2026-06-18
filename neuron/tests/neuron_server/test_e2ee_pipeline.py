# SPDX-License-Identifier: Apache-2.0
"""HS-5 done-criterion: a full E2EE key-relay pipeline through neuron_server.

Uses real libolm. A recipient ("auditbot") publishes its Olm device keys and
one-time keys; a sender claims an OTK, establishes an Olm session, shares a Megolm
room key via an Olm-encrypted ``sendToDevice`` message, and encrypts a room
message with that Megolm session. The recipient then **syncs against
neuron_server**, receives the to-device message, decrypts it, imports the room
key, and decrypts the room message — proving automatic key receipt works end to
end against our server. The server only relays; it never decrypts.
"""

from __future__ import annotations

import json
from pathlib import Path

import olm
from fastapi.testclient import TestClient

from neuron_crypto.megolm import MegolmSessionStore
from neuron_crypto.olm_device import MEGOLM_ALGORITHM, OLM_ALGORITHM, OlmDevice
from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_B = "/_matrix/client/v3"


def _client(tmp_path: Path) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    return TestClient(create_app(settings))


def _register(client: TestClient, username: str) -> tuple[str, str, str]:
    challenge = client.post(_REG, json={"username": username, "password": "pw-123456"})
    session = challenge.json()["session"]
    out = client.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    ).json()
    return out["access_token"], out["user_id"], out["device_id"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_full_e2ee_key_relay_pipeline(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        a_token, a_user, a_device = _register(client, "auditbot")
        s_token, s_user, _s_device = _register(client, "sender")

        # 1. Recipient publishes its Olm device keys + one-time keys.
        device = OlmDevice(a_user, a_device)
        one_time_keys = device.generate_one_time_keys(5)
        upload = client.post(
            f"{_B}/keys/upload",
            headers=_h(a_token),
            json={"device_keys": device.device_keys(), "one_time_keys": one_time_keys},
        ).json()
        assert upload["one_time_key_counts"]["signed_curve25519"] == 5

        # 2. Sender looks up the recipient's identity key and claims an OTK.
        queried = client.post(
            f"{_B}/keys/query", headers=_h(s_token), json={"device_keys": {a_user: []}}
        ).json()
        recipient_curve = queried["device_keys"][a_user][a_device]["keys"][f"curve25519:{a_device}"]
        assert recipient_curve == device.curve25519

        claim = client.post(
            f"{_B}/keys/claim",
            headers=_h(s_token),
            json={"one_time_keys": {a_user: {a_device: "signed_curve25519"}}},
        ).json()
        otk_key = next(iter(claim["one_time_keys"][a_user][a_device].values()))["key"]

        # 3. Sender builds a Megolm session and shares it via an Olm to-device msg.
        sender_account = olm.Account()
        sender_curve = sender_account.identity_keys["curve25519"]
        outbound = olm.OutboundGroupSession()
        room_id = "!r:neuron.local"
        room_key_payload = {
            "type": "m.room_key",
            "content": {
                "algorithm": MEGOLM_ALGORITHM,
                "room_id": room_id,
                "session_id": outbound.id,
                "session_key": outbound.session_key,
            },
            "recipient": a_user,
            "recipient_keys": {"ed25519": device.ed25519},
        }
        olm_session = olm.OutboundSession(sender_account, recipient_curve, otk_key)
        wrapped = olm_session.encrypt(json.dumps(room_key_payload))
        to_device_content = {
            "algorithm": OLM_ALGORITHM,
            "sender_key": sender_curve,
            "ciphertext": {
                recipient_curve: {"type": wrapped.message_type, "body": wrapped.ciphertext}
            },
        }
        client.put(
            f"{_B}/sendToDevice/m.room.encrypted/t1",
            headers=_h(s_token),
            json={"messages": {a_user: {a_device: to_device_content}}},
        )

        # 4. Sender encrypts a room message with the same Megolm session.
        encrypted_event = {
            "type": "m.room.encrypted",
            "sender": s_user,
            "content": {
                "algorithm": MEGOLM_ALGORITHM,
                "sender_key": sender_curve,
                "session_id": outbound.id,
                "ciphertext": outbound.encrypt(
                    json.dumps(
                        {
                            "type": "m.room.message",
                            "content": {"body": "the secret"},
                            "room_id": room_id,
                        }
                    )
                ),
            },
        }

        # 5. Recipient syncs against neuron_server, decrypts the key, then the message.
        synced = client.get(f"{_B}/sync?timeout=0", headers=_h(a_token)).json()
        assert synced["device_one_time_keys_count"]["signed_curve25519"] == 4  # one claimed
        to_device = synced["to_device"]["events"]
        assert len(to_device) == 1 and to_device[0]["type"] == "m.room.encrypted"

        envelope = to_device[0]["content"]
        for_me = envelope["ciphertext"][device.curve25519]
        payload = device.decrypt_to_device(envelope["sender_key"], for_me["type"], for_me["body"])
        assert payload is not None and payload["type"] == "m.room_key"

        store = MegolmSessionStore()
        store.import_room_key(payload["content"])
        result = store.decrypt_event(encrypted_event)
        assert result.decrypted
        assert result.content == {"body": "the secret"}

        # 6. Acknowledged to-device messages are not redelivered.
        token = synced["next_batch"]
        again = client.get(f"{_B}/sync?since={token}&timeout=0", headers=_h(a_token)).json()
        assert again["to_device"]["events"] == []
