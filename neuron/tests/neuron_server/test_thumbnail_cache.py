# SPDX-License-Identifier: Apache-2.0
"""Server-side thumbnail caching (the ``media_thumbnails`` table + blob store).

A generated thumbnail is stored (row + blob) on first request so an identical
later request is served from the cache WITHOUT re-decoding the original. Covers
local + remote media, the bounded standard-size allowlist, invalidation on delete,
non-image fallthrough, and best-effort caching (a cache-write failure still serves
the thumbnail).
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.media import service as media_service
from neuron_server.storage import media_thumbnails as thumb_store

try:
    from PIL import Image

    _HAVE_PIL = True
except ImportError:  # pragma: no cover - depends on the test environment
    _HAVE_PIL = False

_needs_pil = pytest.mark.skipif(not _HAVE_PIL, reason="Pillow is required to generate thumbnails")

_CS = "/_matrix/client/v3"
_CS1 = "/_matrix/client/v1"


def _png(size: tuple[int, int] = (128, 96), color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


async def _register(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    session = (
        await client.post(f"{_CS}/register", json={"username": username, "password": "pw-123456"})
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


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    data: bytes,
    content_type: str = "image/png",
) -> str:
    resp = await client.post(
        "/_matrix/media/v3/upload?filename=pic.bin",
        headers={**headers, "Content-Type": content_type},
        content=data,
    )
    return resp.json()["content_uri"].rsplit("/", 1)[1]


def _spy_make_thumbnail(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap service.make_thumbnail with a call counter (proves cache hits skip it)."""
    real = media_service.make_thumbnail
    calls = {"n": 0}

    def spy(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(media_service, "make_thumbnail", spy)
    return calls


def _app(tmp_path: Path, name: str = "neuron.local") -> FastAPI:
    return create_app(
        NeuronServerSettings(
            name=name,
            database_url=f"sqlite:///{tmp_path / f'{name}.db'}",
            media_store_path=str(tmp_path / f"{name}-media"),
        )
    )


@_needs_pil
async def test_thumbnail_generated_then_served_from_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://neuron.local"
        )
        try:
            headers = await _register(client, "alice")
            media_id = await _upload(client, headers, _png())
            calls = _spy_make_thumbnail(monkeypatch)

            url = f"{_CS1}/media/thumbnail/neuron.local/{media_id}?width=96&height=96&method=crop"
            r1 = await client.get(url, headers=headers)
            assert r1.status_code == 200
            assert r1.headers["content-type"] == "image/png"
            assert calls["n"] == 1

            # Row + blob are present after the first (generating) request.
            row = await thumb_store.get_thumbnail(
                app.state.db, "neuron.local", media_id, 96, 96, "crop"
            )
            assert row is not None
            assert await app.state.media._store.get(row.cache_key) is not None  # noqa: SLF001

            # Second identical request is served from cache WITHOUT re-decoding.
            r2 = await client.get(url, headers=headers)
            assert r2.status_code == 200
            assert r2.content == r1.content
            assert calls["n"] == 1
        finally:
            await client.aclose()


@_needs_pil
async def test_snap_bounds_variant_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://neuron.local"
        )
        try:
            headers = await _register(client, "alice")
            media_id = await _upload(client, headers, _png())
            _spy_make_thumbnail(monkeypatch)

            # Several odd scale sizes all snap to the same allowlisted variant, so no
            # unbounded row growth: exactly one media_thumbnails row for these.
            for w, h in ((1, 1), (50, 50), (200, 150), (300, 200)):
                resp = await client.get(
                    f"{_CS1}/media/thumbnail/neuron.local/{media_id}"
                    f"?width={w}&height={h}&method=scale",
                    headers=headers,
                )
                assert resp.status_code == 200

            keys = await thumb_store.list_thumbnail_keys(app.state.db, "neuron.local", media_id)
            assert len(keys) == 1
            row = await thumb_store.get_thumbnail(
                app.state.db, "neuron.local", media_id, 320, 240, "scale"
            )
            assert row is not None  # snapped to the smallest scale variant >= request
        finally:
            await client.aclose()


async def test_non_image_serves_original_and_caches_nothing(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://neuron.local"
        )
        try:
            headers = await _register(client, "alice")
            body = b"not an image at all"
            media_id = await _upload(client, headers, body, content_type="text/plain")

            resp = await client.get(
                f"{_CS1}/media/thumbnail/neuron.local/{media_id}?width=96&height=96&method=crop",
                headers=headers,
            )
            assert resp.status_code == 200
            assert resp.content == body  # original served unchanged

            keys = await thumb_store.list_thumbnail_keys(app.state.db, "neuron.local", media_id)
            assert keys == []
        finally:
            await client.aclose()


@_needs_pil
async def test_delete_drops_cached_thumbnails(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://neuron.local"
        )
        try:
            headers = await _register(client, "alice")
            media_id = await _upload(client, headers, _png())
            await client.get(
                f"{_CS1}/media/thumbnail/neuron.local/{media_id}?width=96&height=96&method=crop",
                headers=headers,
            )
            keys = await thumb_store.list_thumbnail_keys(app.state.db, "neuron.local", media_id)
            assert len(keys) == 1
            assert await app.state.media._store.get(keys[0]) is not None  # noqa: SLF001

            assert await app.state.media.delete(media_id) is True

            assert (
                await thumb_store.list_thumbnail_keys(app.state.db, "neuron.local", media_id) == []
            )
            assert await app.state.media._store.get(keys[0]) is None  # noqa: SLF001
        finally:
            await client.aclose()


@_needs_pil
async def test_cache_write_failure_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://neuron.local"
        )
        try:
            headers = await _register(client, "alice")
            media_id = await _upload(client, headers, _png())

            async def boom(*args: object, **kwargs: object) -> None:
                raise RuntimeError("cache table is unavailable")

            monkeypatch.setattr(thumb_store, "create_thumbnail", boom)

            resp = await client.get(
                f"{_CS1}/media/thumbnail/neuron.local/{media_id}?width=96&height=96&method=crop",
                headers=headers,
            )
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "image/png"
            # Nothing cached (the write failed), but the thumbnail was still served.
            assert (
                await thumb_store.list_thumbnail_keys(app.state.db, "neuron.local", media_id) == []
            )
        finally:
            await client.aclose()


def _opener(apps: dict[str, FastAPI]) -> Callable[[str], httpx.AsyncClient]:
    def open_client(server_name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[server_name]),
            base_url=f"https://{server_name}",
        )

    return open_client


@_needs_pil
async def test_remote_thumbnail_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apps = {
        name: _app(tmp_path, name=name) for name in ("a.test", "b.test")
    }
    app_a, app_b = apps["a.test"], apps["b.test"]
    async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
        app_a.state.federation_client.open_client = _opener(apps)
        app_b.state.federation_client.open_client = _opener(apps)

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
            calls = _spy_make_thumbnail(monkeypatch)

            url = f"{_CS1}/media/thumbnail/a.test/{media_id}?width=96&height=96&method=crop"
            r1 = await client_b.get(url, headers=bob_h)
            assert r1.status_code == 200
            assert calls["n"] == 1

            # Cached under the REMOTE origin server.
            row = await thumb_store.get_thumbnail(
                app_b.state.db, "a.test", media_id, 96, 96, "crop"
            )
            assert row is not None

            r2 = await client_b.get(url, headers=bob_h)
            assert r2.status_code == 200
            assert r2.content == r1.content
            assert calls["n"] == 1  # served from cache, not re-decoded
        finally:
            await client_a.aclose()
            await client_b.aclose()
