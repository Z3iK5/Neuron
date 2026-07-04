# SPDX-License-Identifier: Apache-2.0
"""Outbound federation HTTP client (HS-7).

Makes requests to other homeservers, signing them with the ``X-Matrix`` scheme.
Where to connect for a given server name is decided by :func:`pick_base_url`,
honouring the destination's ``/.well-known/matrix/server`` delegation (fetched
once per destination and cached); the ``open_client`` seam lets tests route
requests to an in-process homeserver via an ASGI transport instead of the network.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from neuron_server.crypto.signing import SigningKey
from neuron_server.federation.auth import sign_request
from neuron_server.federation.discovery import fetch_well_known, pick_base_url

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
        # Resolved base URLs, honouring /.well-known/matrix/server delegation.
        self._base_urls: dict[str, str] = {}
        # Public so deployments/tests can override how a server name is reached
        # (e.g. point at an in-process app over an ASGI transport).
        self.open_client: OpenClient = open_client or self._default_open

    def _default_open(self, server_name: str) -> httpx.AsyncClient:
        base_url = self._base_urls.get(server_name) or pick_base_url(server_name, None)
        return httpx.AsyncClient(base_url=base_url, timeout=self._timeout)

    async def _resolve(self, destination: str) -> None:
        """Honour ``destination``'s well-known delegation (cached per destination).

        Only the real network path does the lookup; an overridden ``open_client``
        decides reachability itself.
        """
        if self.open_client != self._default_open or destination in self._base_urls:
            return
        well_known = await fetch_well_known(destination, timeout=self._timeout)
        self._base_urls[destination] = pick_base_url(destination, well_known)

    async def get_json(
        self, destination: str, path: str, *, sign: bool = True
    ) -> dict[str, Any]:
        """GET ``path`` from ``destination`` (X-Matrix signed unless ``sign`` is False)."""
        await self._resolve(destination)
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

    async def put_json(
        self, destination: str, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """PUT ``body`` to ``destination`` at ``path``, signed with X-Matrix."""
        return await self._send_json("PUT", destination, path, body)

    async def post_json(
        self, destination: str, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST ``body`` to ``destination`` at ``path``, signed with X-Matrix."""
        return await self._send_json("POST", destination, path, body)

    async def _send_json(
        self, method: str, destination: str, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        await self._resolve(destination)
        client = self.open_client(destination)
        try:
            header = sign_request(
                method=method,
                uri=path,
                origin=self._origin,
                destination=destination,
                signing_key=self._signing_key,
                content=body,
            )
            response = await client.request(
                method, path, json=body, headers={"Authorization": header}
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
        finally:
            await client.aclose()
