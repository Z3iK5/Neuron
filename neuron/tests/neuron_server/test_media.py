# SPDX-License-Identifier: Apache-2.0
"""Tests for the neuron_server media repository (HS-4)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"


def _client(tmp_path: Path, *, max_upload_bytes: int = 50 * 1024 * 1024) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        media_store_path=str(tmp_path / "media"),
        max_upload_bytes=max_upload_bytes,
    )
    return TestClient(create_app(settings))


def _register(client: TestClient, username: str = "alice") -> str:
    challenge = client.post(_REG, json={"username": username, "password": "pw-123456"})
    session = challenge.json()["session"]
    result = client.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    )
    return result.json()["access_token"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _png(size: tuple[int, int] = (64, 64), color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _upload(client: TestClient, token: str, data: bytes, content_type: str) -> str:
    resp = client.post(
        "/_matrix/media/v3/upload",
        headers={**_h(token), "Content-Type": content_type},
        params={"filename": "test.png"},
        content=data,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["content_uri"]


def _media_id(content_uri: str) -> str:
    return content_uri.rsplit("/", 1)[1]


def test_upload_download_roundtrip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client)
        png = _png()
        uri = _upload(client, token, png, "image/png")
        assert uri.startswith("mxc://neuron.local/")
        media_id = _media_id(uri)

        resp = client.get(
            f"/_matrix/client/v1/media/download/neuron.local/{media_id}", headers=_h(token)
        )
        assert resp.status_code == 200
        assert resp.content == png
        assert resp.headers["content-type"].startswith("image/png")

        # The legacy v3 download path works too.
        legacy = client.get(
            f"/_matrix/media/v3/download/neuron.local/{media_id}", headers=_h(token)
        )
        assert legacy.status_code == 200 and legacy.content == png


def test_download_requires_auth(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client)
        media_id = _media_id(_upload(client, token, _png(), "image/png"))
        resp = client.get(f"/_matrix/client/v1/media/download/neuron.local/{media_id}")
        assert resp.status_code == 401


def test_config_reports_max_upload(tmp_path: Path) -> None:
    with _client(tmp_path, max_upload_bytes=1234) as client:
        token = _register(client)
        resp = client.get("/_matrix/client/v1/media/config", headers=_h(token))
        assert resp.status_code == 200
        assert resp.json()["m.upload.size"] == 1234


def test_thumbnail_returns_smaller_image(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client)
        media_id = _media_id(_upload(client, token, _png((128, 128)), "image/png"))
        resp = client.get(
            f"/_matrix/client/v1/media/thumbnail/neuron.local/{media_id}",
            headers=_h(token),
            params={"width": 16, "height": 16, "method": "scale"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")
        thumb = Image.open(BytesIO(resp.content))
        assert thumb.width <= 16 and thumb.height <= 16


def test_unknown_media_is_not_found(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client)
        resp = client.get(
            "/_matrix/client/v1/media/download/neuron.local/deadbeefdeadbeef", headers=_h(token)
        )
        assert resp.status_code == 404 and resp.json()["errcode"] == "M_NOT_FOUND"


def test_remote_media_is_not_available(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token = _register(client)
        media_id = _media_id(_upload(client, token, _png(), "image/png"))
        resp = client.get(
            f"/_matrix/client/v1/media/download/other.example/{media_id}", headers=_h(token)
        )
        assert resp.status_code == 404


def test_upload_too_large_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path, max_upload_bytes=16) as client:
        token = _register(client)
        resp = client.post(
            "/_matrix/media/v3/upload",
            headers={**_h(token), "Content-Type": "application/octet-stream"},
            content=b"x" * 100,
        )
        assert resp.status_code == 413 and resp.json()["errcode"] == "M_TOO_LARGE"
