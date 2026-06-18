# SPDX-License-Identifier: Apache-2.0
"""Outbound federation HTTP client (HS-7).

Makes requests to other homeservers, signing them with the ``X-Matrix`` scheme.
Where to connect for a given server name is decided by :func:`pick_base_url`; the
``open_client`` seam lets tests route requests to an in-process homeserver via an
ASGI transport instead of the network.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from neuron_server.crypto.signing import SigningKey
from neuron_server.federation.auth import sign_request
from neuron_server.federation.discovery import pick_base_url

OpenClient = Callable[[str], httpx.AsyncClient]


class FederationClient:
    """Signs and sends outbound federation requests."""

    def __init__(
        self,
        origin_name: str,
        signing_key: SigningKey,
        *,
        open_client: OpenClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._origin = origin_name
        self._signing_key = signing_key
        self._timeout = timeout
        # Public so deployments/tests can override how a server name is reached
        # (e.g. point at an in-process app over an ASGI transport).
        self.open_client: OpenClient = open_client or self._default_open

    def _default_open(self, server_name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=pick_base_url(server_name, None), timeout=self._timeout
        )

    async def get_json(
        self, destination: str, path: str, *, sign: bool = True
    ) -> dict[str, Any]:
        """GET ``path`` from ``destination`` (X-Matrix signed unless ``sign`` is False)."""
        client = self.open_client(destination)
        try:
            headers: dict[str, str] = {}
            if sign:
                headers["Authorization"] = sign_request(
                    method="GET",
                    uri=path,
                    origin=self._origin,
                    destination=destination,
                    signing_key=self._signing_key,
                )
            response = await client.get(path, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        finally:
            await client.aclose()
