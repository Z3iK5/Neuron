# SPDX-License-Identifier: Apache-2.0
"""Tests for the neuron_server ASGI app (HS-0: spec discovery + DB lifespan).

These use FastAPI's ``TestClient`` with a temp-file SQLite database, so no
external services are needed. Entering the ``TestClient`` context manager runs
the app's lifespan (connect + migrate + identity guard).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.spec import SUPPORTED_SPEC_VERSIONS


def _client(
    tmp_path: Path,
    *,
    name: str = "neuron.local",
    public_base_url: str = "http://localhost:8008",
) -> TestClient:
    settings = NeuronServerSettings(
        name=name,
        public_base_url=public_base_url,
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
    )
    return TestClient(create_app(settings))


def test_versions_endpoint(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/_matrix/client/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["versions"] == list(SUPPORTED_SPEC_VERSIONS)
    assert isinstance(body["unstable_features"], dict)


def test_well_known_client_advertises_base_url(tmp_path: Path) -> None:
    with _client(tmp_path, public_base_url="https://matrix.neuron.local") as client:
        resp = client.get("/.well-known/matrix/client")
    assert resp.status_code == 200
    assert resp.json()["m.homeserver"]["base_url"] == "https://matrix.neuron.local"


def test_health(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/health")
    assert resp.status_code == 200


def test_unknown_matrix_endpoint_returns_m_unrecognized(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/_matrix/client/v3/does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["errcode"] == "M_UNRECOGNIZED"


def test_server_identity_is_persisted_and_guarded(tmp_path: Path) -> None:
    # First start records the configured server name.
    with _client(tmp_path, name="neuron.local") as client:
        assert client.get("/health").status_code == 200

    # Restarting against the same database under a different name must refuse.
    with pytest.raises(RuntimeError):
        with _client(tmp_path, name="changed.example"):
            pass
