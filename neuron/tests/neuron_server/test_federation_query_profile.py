# SPDX-License-Identifier: Apache-2.0
"""Federation profile queries (``/_matrix/federation/v1/query/profile``).

Inbound: we serve our local users' displayname/avatar_url to properly signed
federation requests. Outbound: a client asking this server for a *remote* user's
profile gets the answer fetched over federation (with a short in-process cache),
and an unreachable remote reads as ``M_NOT_FOUND``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_CS = "/_matrix/client/v3"
_QUERY = "/_matrix/federation/v1/query/profile"


def _opener(apps: dict[str, FastAPI]):  # noqa: ANN202 - test helper
    def open_client(server_name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[server_name]),
            base_url=f"https://{server_name}",
        )

    return open_client


def _broken_opener(server_name: str) -> httpx.AsyncClient:
    raise ConnectionError(f"{server_name} is unreachable")


async def _register(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    session = (
        await client.post(
            f"{_CS}/register", json={"username": username, "password": "pw-123456"}
        )
    ).json()["session"]
    out = await client.post(
        f"{_CS}/register",
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    )
    return {"Authorization": f"Bearer {out.json()['access_token']}"}


@pytest.fixture
def two_apps(tmp_path: Path) -> dict[str, FastAPI]:
    return {
        name: create_app(
            NeuronServerSettings(
                name=name, database_url=f"sqlite:///{tmp_path / f'{name}.db'}"
            )
        )
        for name in ("a.test", "b.test")
    }


async def test_query_profile_inbound_and_outbound(two_apps: dict[str, FastAPI]) -> None:
    app_a, app_b = two_apps["a.test"], two_apps["b.test"]
    async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
        app_a.state.federation_client.open_client = _opener(two_apps)
        app_b.state.federation_client.open_client = _opener(two_apps)

        client_a = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        )
        client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
        )
        try:
            alice_h = await _register(client_a, "alice")
            await client_a.put(
                f"{_CS}/profile/@alice:a.test/displayname",
                headers=alice_h,
                json={"displayname": "Alice"},
            )
            await client_a.put(
                f"{_CS}/profile/@alice:a.test/avatar_url",
                headers=alice_h,
                json={"avatar_url": "mxc://a.test/xyz"},
            )

            fed_b = app_b.state.federation_client

            # Inbound (signed): both fields by default.
            full = await fed_b.get_json("a.test", f"{_QUERY}?user_id=@alice:a.test")
            assert full == {"displayname": "Alice", "avatar_url": "mxc://a.test/xyz"}

            # field= narrows the answer to just that field.
            one = await fed_b.get_json(
                "a.test", f"{_QUERY}?user_id=@alice:a.test&field=displayname"
            )
            assert one == {"displayname": "Alice"}

            # Unknown local user and non-local user are both M_NOT_FOUND.
            for bad in ("@ghost:a.test", "@bob:b.test"):
                with pytest.raises(httpx.HTTPStatusError) as err:
                    await fed_b.get_json("a.test", f"{_QUERY}?user_id={bad}")
                assert err.value.response.status_code == 404

            # An unsigned request is rejected.
            raw = await client_a.get(f"{_QUERY}?user_id=@alice:a.test")
            assert raw.status_code == 401

            # Outbound: a client on B asks B for alice's (remote) profile.
            out = (await client_b.get(f"{_CS}/profile/@alice:a.test")).json()
            assert out == {"displayname": "Alice", "avatar_url": "mxc://a.test/xyz"}
            out = (await client_b.get(f"{_CS}/profile/@alice:a.test/displayname")).json()
            assert out == {"displayname": "Alice"}
            out = (await client_b.get(f"{_CS}/profile/@alice:a.test/avatar_url")).json()
            assert out == {"avatar_url": "mxc://a.test/xyz"}

            # Cache: a change on A is not seen through B's cache within the TTL.
            await client_a.put(
                f"{_CS}/profile/@alice:a.test/displayname",
                headers=alice_h,
                json={"displayname": "Alice v2"},
            )
            cached = (await client_b.get(f"{_CS}/profile/@alice:a.test")).json()
            assert cached["displayname"] == "Alice"

            # Unreachable remote → 404 M_NOT_FOUND per spec.
            app_b.state.federation_client.open_client = _broken_opener
            gone = await client_b.get(f"{_CS}/profile/@who:offline.test")
            assert gone.status_code == 404
            assert gone.json()["errcode"] == "M_NOT_FOUND"
        finally:
            await client_a.aclose()
            await client_b.aclose()
