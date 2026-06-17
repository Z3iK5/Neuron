# SPDX-License-Identifier: Apache-2.0
"""Tests for neuron_core.csapi_client.MatrixClient (uses a mock transport)."""

from __future__ import annotations

import httpx
import pytest

from neuron_core.csapi_client import MatrixClient
from neuron_core.errors import MatrixError

BASE = "http://hs.test"
ROOM = "!room:hs.test"


def _client(handler: object) -> MatrixClient:
    mock = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return MatrixClient(BASE, "bot-token", client=mock)


async def test_whoami() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_matrix/client/v3/account/whoami"
        return httpx.Response(200, json={"user_id": "@bot:hs.test"})

    async with _client(handler) as bot:
        assert (await bot.whoami())["user_id"] == "@bot:hs.test"


async def test_kick_sends_user_and_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/_matrix/client/v3/rooms/{ROOM}/kick"
        import json as _json

        body = _json.loads(request.content)
        assert body == {"user_id": "@bad:hs.test", "reason": "spam"}
        return httpx.Response(200, json={})

    async with _client(handler) as bot:
        await bot.kick(ROOM, "@bad:hs.test", reason="spam")


async def test_redact_event_returns_event_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path.startswith(f"/_matrix/client/v3/rooms/{ROOM}/redact/$evt/")
        return httpx.Response(200, json={"event_id": "$redaction"})

    async with _client(handler) as bot:
        assert await bot.redact_event(ROOM, "$evt", reason="cleanup") == "$redaction"


async def test_error_raises_matrix_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errcode": "M_FORBIDDEN", "error": "no"})

    async with _client(handler) as bot:
        with pytest.raises(MatrixError) as excinfo:
            await bot.kick(ROOM, "@x:hs.test")
    assert excinfo.value.status_code == 403
    assert excinfo.value.errcode == "M_FORBIDDEN"


async def test_sync_passes_since_and_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_matrix/client/v3/sync"
        assert request.url.params["since"] == "tok1"
        assert request.url.params["timeout"] == "0"
        return httpx.Response(200, json={"next_batch": "tok2", "rooms": {}})

    async with _client(handler) as bot:
        result = await bot.sync(since="tok1", timeout_ms=0)
    assert result["next_batch"] == "tok2"


async def test_join_room() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/_matrix/client/v3/join/{ROOM}"
        return httpx.Response(200, json={"room_id": ROOM})

    async with _client(handler) as bot:
        assert (await bot.join_room(ROOM))["room_id"] == ROOM


async def test_keys_upload_sends_device_and_one_time_keys() -> None:
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/_matrix/client/v3/keys/upload"
        body = _json.loads(request.content)
        assert "device_keys" in body
        assert "one_time_keys" in body
        return httpx.Response(200, json={"one_time_key_counts": {"signed_curve25519": 5}})

    async with _client(handler) as bot:
        result = await bot.keys_upload(device_keys={"user_id": "@b:hs"}, one_time_keys={"k": 1})
    assert result["one_time_key_counts"]["signed_curve25519"] == 5


def test_auth_header_is_set() -> None:
    bot = MatrixClient(BASE, "bot-token")
    assert bot._client.headers["Authorization"] == "Bearer bot-token"
