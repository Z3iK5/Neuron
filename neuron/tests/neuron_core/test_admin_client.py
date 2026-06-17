# SPDX-License-Identifier: Apache-2.0
"""Tests for neuron_core.admin_client.

These tests do NOT need a running Synapse: we use httpx's ``MockTransport`` to
simulate the homeserver's responses. This lets us assert that the client builds
the right URLs and query parameters, parses responses correctly, and turns error
responses into ``SynapseAdminError``.
"""

from __future__ import annotations

import httpx
import pytest

from neuron_core.admin_client import SynapseAdminClient
from neuron_core.errors import SynapseAdminError

BASE_URL = "http://synapse.test"


def _make_client(handler: object) -> SynapseAdminClient:
    """Build a SynapseAdminClient backed by a mock transport."""
    mock_client = httpx.AsyncClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    return SynapseAdminClient(BASE_URL, "test-token", client=mock_client)


async def test_get_server_version_parses_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_synapse/admin/v1/server_version"
        return httpx.Response(200, json={"server_version": "1.155.0", "python_version": "3.11.9"})

    async with _make_client(handler) as admin:
        result = await admin.get_server_version()

    assert result["server_version"] == "1.155.0"
    assert result["python_version"] == "3.11.9"


async def test_list_users_builds_params_and_parses_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_synapse/admin/v2/users"
        # Query params should reflect our typed arguments.
        assert request.url.params["limit"] == "5"
        assert request.url.params["name"] == "alice"
        assert request.url.params["deactivated"] == "false"
        return httpx.Response(
            200,
            json={
                "users": [{"name": "@alice:synapse.test", "admin": False}],
                "total": 1,
                "next_token": "100",
            },
        )

    async with _make_client(handler) as admin:
        page = await admin.list_users(limit=5, name="alice", deactivated=False)

    assert page.total == 1
    assert page.next_token == "100"
    assert page.users[0]["name"] == "@alice:synapse.test"


async def test_get_user_uses_user_id_in_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_synapse/admin/v2/users/@bob:synapse.test"
        return httpx.Response(200, json={"name": "@bob:synapse.test", "deactivated": False})

    async with _make_client(handler) as admin:
        user = await admin.get_user("@bob:synapse.test")

    assert user["name"] == "@bob:synapse.test"


async def test_error_response_raises_synapse_admin_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN", "error": "Invalid token"})

    async with _make_client(handler) as admin:
        with pytest.raises(SynapseAdminError) as excinfo:
            await admin.get_server_version()

    assert excinfo.value.status_code == 401
    assert excinfo.value.errcode == "M_UNKNOWN_TOKEN"
    assert excinfo.value.message == "Invalid token"


def test_self_built_client_sets_authorization_header() -> None:
    # When we don't inject a client, the admin client builds one with a Bearer token.
    admin = SynapseAdminClient(BASE_URL, "my-token")
    assert admin._client.headers["Authorization"] == "Bearer my-token"
