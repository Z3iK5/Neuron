# SPDX-License-Identifier: Apache-2.0
"""Tests for the optional Prometheus /metrics endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings


def _settings(tmp_path: Path, **kw: Any) -> NeuronServerSettings:
    return NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}", **kw
    )


def test_metrics_disabled_has_no_endpoint(tmp_path: Path) -> None:
    # Default: no /metrics route, and prometheus_client is never imported.
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/metrics").status_code == 404


def test_metrics_enabled_exposes_prometheus(tmp_path: Path) -> None:
    pytest.importorskip("prometheus_client")
    with TestClient(create_app(_settings(tmp_path, metrics_enabled=True))) as client:
        client.get("/health")
        client.get("/health")
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        assert "neuron_http_requests_total" in body
        assert "neuron_http_request_duration_seconds" in body
        # Labelled by the route TEMPLATE (low cardinality), and the scrape endpoint
        # itself isn't counted.
        assert 'route="/health"' in body
        assert 'route="/metrics"' not in body


def test_metrics_enabled_multiple_apps_no_registry_collision(tmp_path: Path) -> None:
    pytest.importorskip("prometheus_client")
    # Each app uses its own CollectorRegistry, so building several in one process
    # must not raise prometheus_client's "Duplicated timeseries" error.
    create_app(_settings(tmp_path, metrics_enabled=True))
    create_app(_settings(tmp_path, metrics_enabled=True))
