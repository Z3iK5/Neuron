# SPDX-License-Identifier: Apache-2.0
"""Internal HTTP helpers shared by the Matrix API clients.

Both the Admin API client and the Client-Server API client need the same
plumbing — an authenticated ``httpx.AsyncClient`` with a managed lifecycle,
and a way to turn an ``httpx.Response`` into either parsed JSON or a raised
error. That logic lives here so it is written once.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import httpx

from neuron_core.errors import MatrixApiError


def ok_json(response: httpx.Response, error_cls: type[MatrixApiError]) -> dict[str, Any]:
    """Return the parsed JSON body, or raise ``error_cls`` on a non-2xx response.

    Successful responses with an empty body return an empty dict (some endpoints
    reply ``200`` with no JSON). Non-dict JSON is wrapped as ``{"data": ...}``.
    """
    if response.is_success:
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    errcode: str | None = None
    message: str | None = None
    try:
        body = response.json()
        errcode = body.get("errcode")
        message = body.get("error")
    except (ValueError, AttributeError):
        message = response.text or None
    raise error_cls(response.status_code, errcode=errcode, message=message)


class BaseApiClient:
    """Shared HTTP-client lifecycle for the typed Matrix API clients.

    Subclasses set ``_error_cls`` to the error type raised on non-2xx responses.
    """

    _error_cls: type[MatrixApiError] = MatrixApiError

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create the client.

        :param base_url: homeserver base URL, e.g. ``http://localhost:8008``.
        :param access_token: the access token sent as a Bearer token.
        :param timeout: per-request timeout in seconds.
        :param client: an optional pre-built ``httpx.AsyncClient`` (used by tests
            to inject a mock transport). When omitted we build our own.
        """
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    # --- lifecycle ----------------------------------------------------------
    async def aclose(self) -> None:
        """Close the underlying HTTP client (only if we created it)."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # --- internal helpers ---------------------------------------------------
    def _ok_json(self, response: httpx.Response) -> dict[str, Any]:
        """Return the parsed JSON body, or raise ``_error_cls`` on failure."""
        return ok_json(response, self._error_cls)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.request(method, path, params=params, json=json)
        return self._ok_json(response)
