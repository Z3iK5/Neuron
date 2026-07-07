# SPDX-License-Identifier: Apache-2.0
"""Remote media fetch-and-cache over federation (authenticated media, spec v1.11).

Inbound: we serve our local media to signed federation requests as a
``multipart/mixed`` body (401 when unsigned). Outbound: a client on server B that
downloads media hosted on server A gets the bytes fetched over federation and
cached, so a second download is served locally without another federation call.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.media.multipart import parse_multipart

_CS = "/_matrix/client/v3"
_CS1 = "/_matrix/client/v1"
_FED_DL = "/_matrix/federation/v1/media/download"


def _opener(apps: dict[str, FastAPI]) -> Callable[[str], httpx.AsyncClient]:
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


def _png(color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (64, 48), color).save(out, format="PNG")
    return out.getvalue()


async def _upload(client: httpx.AsyncClient, headers: dict[str, str], data: bytes) -> str:
    resp = await client.post(
        "/_matrix/media/v3/upload?filename=pic.png",
        headers={**headers, "Content-Type": "image/png"},
        content=data,
    )
    # mxc://a.test/<media_id> -> media_id
    return resp.json()["content_uri"].rsplit("/", 1)[1]


@pytest.fixture
def two_apps(tmp_path: Path) -> dict[str, FastAPI]:
    return {
        name: create_app(
            NeuronServerSettings(
                name=name,
                database_url=f"sqlite:///{tmp_path / f'{name}.db'}",
                media_store_path=str(tmp_path / f"{name}-media"),
            )
        )
        for name in ("a.test", "b.test")
    }


async def test_remote_media_fetch_cache_and_inbound(two_apps: dict[str, FastAPI]) -> None:
    app_a, app_b = two_apps["a.test"], two_apps["b.test"]
    async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
        app_a.state.federation_client.open_client = _opener(two_apps)
        app_b.state.federation_client.open_client = _opener(two_apps)

        # Spy on B's outbound media fetch so we can prove the cache prevents re-fetch.
        fed_b = app_b.state.federation_client
        real_get_media = fed_b.get_media
        calls = {"n": 0}

        async def counting_get_media(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            return await real_get_media(*args, **kwargs)

        fed_b.get_media = counting_get_media  # type: ignore[method-assign]

        client_a = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        )
        client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
        )
        try:
            alice_h = await _register(client_a, "alice")
            bob_h = await _register(client_b, "bob")
            image = _png()
            media_id = await _upload(client_a, alice_h, image)

            # --- Outbound: B downloads A's media over federation ---
            dl = await client_b.get(
                f"{_CS1}/media/download/a.test/{media_id}", headers=bob_h
            )
            assert dl.status_code == 200
            assert dl.content == image
            assert dl.headers["content-type"] == "image/png"
            assert calls["n"] == 1

            # --- Cache hit: a second download does NOT re-fetch over federation ---
            again = await client_b.get(
                f"{_CS1}/media/download/a.test/{media_id}", headers=bob_h
            )
            assert again.status_code == 200
            assert again.content == image
            assert calls["n"] == 1

            # --- Remote thumbnail works (thumbnailed locally from the cached bytes) ---
            thumb = await client_b.get(
                f"{_CS1}/media/thumbnail/a.test/{media_id}?width=32&height=32&method=crop",
                headers=bob_h,
            )
            assert thumb.status_code == 200
            assert thumb.headers["content-type"] == "image/png"
            assert Image.open(BytesIO(thumb.content)).size[0] <= 32
            assert calls["n"] == 1  # still served from cache

            # --- Inbound: the federation media endpoint returns multipart/mixed ---
            ct, body = await real_get_media(
                "a.test", f"{_FED_DL}/{media_id}", max_bytes=10_000_000
            )
            assert ct.startswith("multipart/mixed")
            parts = parse_multipart(ct, body)
            assert len(parts) == 2
            assert parts[0][0]["content-type"] == "application/json"
            assert parts[1][0]["content-type"] == "image/png"
            assert parts[1][1] == image

            # --- Inbound requires a signed request: unsigned is 401 ---
            unsigned = await client_a.get(f"{_FED_DL}/{media_id}")
            assert unsigned.status_code == 401

            # --- Unknown local media is M_NOT_FOUND over federation ---
            with pytest.raises(httpx.HTTPStatusError) as err:
                await real_get_media("a.test", f"{_FED_DL}/deadbeef", max_bytes=10_000)
            assert err.value.response.status_code == 404
        finally:
            await client_a.aclose()
            await client_b.aclose()


async def test_remote_media_unreachable_is_not_found(two_apps: dict[str, FastAPI]) -> None:
    app_a, app_b = two_apps["a.test"], two_apps["b.test"]
    async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
        app_b.state.federation_client.open_client = _broken_opener
        client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
        )
        try:
            bob_h = await _register(client_b, "bob")
            gone = await client_b.get(
                f"{_CS1}/media/download/a.test/whatever", headers=bob_h
            )
            assert gone.status_code == 404
            assert gone.json()["errcode"] == "M_NOT_FOUND"
        finally:
            await client_b.aclose()


async def test_remote_media_oversized_is_rejected(two_apps: dict[str, FastAPI]) -> None:
    app_a, app_b = two_apps["a.test"], two_apps["b.test"]
    async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
        app_a.state.federation_client.open_client = _opener(two_apps)
        app_b.state.federation_client.open_client = _opener(two_apps)
        # Cap B's remote-media size below the uploaded file so the fetch is refused.
        app_b.state.media._max_remote_media_bytes = 8  # noqa: SLF001

        client_a = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="https://a.test"
        )
        client_b = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="https://b.test"
        )
        try:
            alice_h = await _register(client_a, "alice")
            bob_h = await _register(client_b, "bob")
            media_id = await _upload(client_a, alice_h, _png())

            resp = await client_b.get(
                f"{_CS1}/media/download/a.test/{media_id}", headers=bob_h
            )
            assert resp.status_code == 502
            assert resp.json()["errcode"] == "M_TOO_LARGE"
            # Nothing was cached (the cap is enforced before storing).
            from neuron_server.storage import remote_media as rm

            assert await rm.get_remote_media(app_b.state.db, "a.test", media_id) is None
        finally:
            await client_a.aclose()
            await client_b.aclose()
