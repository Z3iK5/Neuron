# SPDX-License-Identifier: Apache-2.0
"""Optional Prometheus metrics.

When ``NEURON_SERVER_METRICS_ENABLED`` is set, :func:`install_metrics` adds a
``/metrics`` endpoint and a middleware that records per-request count and latency,
labelled by method, the matched **route template** (low cardinality — random path
segments share one ``route``), and status. ``prometheus_client`` is imported
lazily here, so a server with metrics disabled (the default, including the
desktop) needs neither the dependency nor the endpoint.

Each app gets its own ``CollectorRegistry`` so creating several apps in one process
(e.g. in tests) never collides on the global default registry.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from fastapi import FastAPI

    from neuron_server.config import NeuronServerSettings

_NAMESPACE = "neuron"
_METRICS_PATH = "/metrics"


def install_metrics(app: FastAPI, settings: NeuronServerSettings) -> None:
    """Add the ``/metrics`` endpoint + request-metrics middleware, if enabled."""
    if not settings.metrics_enabled:
        return

    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Histogram,
        ProcessCollector,
        generate_latest,
    )

    registry = CollectorRegistry()
    # Standard process metrics (memory, CPU, fds) alongside our HTTP metrics.
    ProcessCollector(registry=registry)
    requests_total = Counter(
        f"{_NAMESPACE}_http_requests_total",
        "Total HTTP requests handled.",
        labelnames=("method", "route", "status"),
        registry=registry,
    )
    request_duration = Histogram(
        f"{_NAMESPACE}_http_request_duration_seconds",
        "HTTP request duration in seconds.",
        labelnames=("method", "route"),
        registry=registry,
    )

    @app.middleware("http")
    async def _record_request(request: Request, call_next):  # type: ignore[no-untyped-def]
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        # The matched route's TEMPLATE (e.g. /_matrix/client/v3/rooms/{room_id}/...),
        # so high-cardinality path segments (IDs) don't explode the series; an
        # unmatched path (404s on random URLs) collapses to a single label.
        route = request.scope.get("route")
        label = getattr(route, "path", None) or "unmatched"
        if label != _METRICS_PATH:  # don't measure the scrape itself
            requests_total.labels(request.method, label, str(response.status_code)).inc()
            request_duration.labels(request.method, label).observe(duration)
        return response

    @app.get(_METRICS_PATH, include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
