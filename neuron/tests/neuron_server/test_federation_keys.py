# SPDX-License-Identifier: Apache-2.0
"""Tests for the Server-Server key API (``/_matrix/key/v2/server``) — HS-7.

The decisive check mirrors what a remote homeserver does: fetch the document and
verify its self-signature using only the verify key it publishes.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.crypto.signing import verify_signed_json


def _settings(tmp_path: Path, **extra: object) -> NeuronServerSettings:
    return NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}", **extra
    )


def test_published_key_document_is_self_verifiable(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        body = client.get("/_matrix/key/v2/server").json()

    assert body["server_name"] == "neuron.local"
    assert body["old_verify_keys"] == {}
    assert body["valid_until_ts"] > int(time.time() * 1000)

    key_id = next(iter(body["verify_keys"]))
    assert key_id.startswith("ed25519:")
    verify_key_b64 = body["verify_keys"][key_id]["key"]

    # Exactly the check a federating peer performs.
    assert verify_signed_json(
        body, server_name="neuron.local", verify_key_base64=verify_key_b64, key_id=key_id
    )


def test_key_id_endpoint_returns_same_document(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        full = client.get("/_matrix/key/v2/server").json()
        key_id = next(iter(full["verify_keys"]))
        scoped = client.get(f"/_matrix/key/v2/server/{key_id}").json()
    assert scoped["server_name"] == full["server_name"]
    assert scoped["verify_keys"] == full["verify_keys"]


def test_signing_key_persists_across_restarts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        first = set(client.get("/_matrix/key/v2/server").json()["verify_keys"])
    # Fresh app, same database: the federation identity must be stable.
    with TestClient(create_app(settings)) as client:
        second = set(client.get("/_matrix/key/v2/server").json()["verify_keys"])
    assert first == second


def test_signing_key_file_backend(tmp_path: Path) -> None:
    key_path = tmp_path / "signing.key"
    settings = _settings(tmp_path, signing_key_path=str(key_path))
    with TestClient(create_app(settings)) as client:
        key_id = next(iter(client.get("/_matrix/key/v2/server").json()["verify_keys"]))

    assert key_path.exists()
    assert key_path.read_text().startswith("ed25519 ")
    # The version embedded in the file matches the published key id.
    assert key_path.read_text().split()[1] == key_id.split(":", 1)[1]
