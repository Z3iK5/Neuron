"""Internal HTTP helpers shared by the Matrix API clients.

Both the Synapse Admin API client and the Client-Server API client need to turn
an ``httpx.Response`` into either parsed JSON or a raised error. That logic lives
here so it is written once.
"""

from __future__ import annotations

from typing import Any

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
