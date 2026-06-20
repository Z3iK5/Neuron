# SPDX-License-Identifier: Apache-2.0
"""Console passkey (WebAuthn) sign-in.

The browser ceremony and the cryptographic attestation/assertion checks belong to
``py_webauthn`` + a real authenticator, so here we test the DB store, the options
endpoints (which need no authenticator), the auth gating, and the route wiring with
the verify step mocked.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuron_server import passkeys as pk
from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.storage import admin as admin_store
from neuron_server.storage.database import connect_database
from neuron_server.storage.migrations import run_migrations

_REG = "/console/passkeys"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            NeuronServerSettings(
                name="neuron.local",
                database_url=f"sqlite:///{tmp_path / 'hs.db'}",
                first_user_admin=True,
                public_base_url="http://localhost:8008",
            )
        )
    )


def _login(client: TestClient) -> None:
    client.post("/get-started", data={"username": "admin", "password": "s3cret-password"})
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/console/login").text)
    assert token
    client.post(
        "/console/login",
        data={"username": "admin", "password": "s3cret-password", "csrf_token": token.group(1)},
        follow_redirects=False,
    )


def _page_csrf(client: TestClient) -> str:
    m = re.search(r'window\.NEURON_CSRF="([^"]+)"', client.get(_REG).text)
    assert m, "csrf token not exposed on the passkeys page"
    return m.group(1)


def test_passkey_store_roundtrip(tmp_path: Path) -> None:
    async def run() -> None:
        db = connect_database(f"sqlite:///{tmp_path / 'pk.db'}")
        await db.connect()
        await run_migrations(db)
        await admin_store.add_passkey(
            db, credential_id="AAAA", owner="@a:x", public_key="UEs", sign_count=0,
            label="key", ts=1,
        )
        assert [k["credential_id"] for k in await admin_store.list_passkeys(db, "@a:x")] == ["AAAA"]
        assert await admin_store.all_passkey_ids(db) == ["AAAA"]
        await admin_store.set_passkey_sign_count(db, "AAAA", 7)
        got = await admin_store.get_passkey(db, "AAAA")
        assert got is not None and got["sign_count"] == 7 and got["owner"] == "@a:x"
        await admin_store.remove_passkey(db, "@a:x", "AAAA")
        assert await admin_store.get_passkey(db, "AAAA") is None
        await db.disconnect()

    asyncio.run(run())


def test_register_requires_login(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        # No session and (after signup) the page is gated; unauthenticated -> redirect.
        client.post("/get-started", data={"username": "admin", "password": "s3cret-password"})
        resp = client.post("/console/passkeys/register/options", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/console/login"


def test_register_options_returns_webauthn_json(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        csrf = _page_csrf(client)
        resp = client.post(
            "/console/passkeys/register/options", headers={"X-CSRF-Token": csrf}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "challenge" in body and body["rp"]["id"] == "testserver"  # TestClient host


def test_register_then_login_with_passkey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path) as client:
        _login(client)
        csrf = _page_csrf(client)
        # Seed the challenge via the real options endpoint.
        client.post("/console/passkeys/register/options", headers={"X-CSRF-Token": csrf})

        # Mock the attestation verification (no real authenticator in a test).
        monkeypatch.setattr(
            pk, "verify_registration",
            lambda *a, **k: {
                "credential_id": "AAAA", "public_key": "UEs", "sign_count": 0,
                "label": "MacBook", "created_ts": 1,
            },
        )
        ok = client.post(
            "/console/passkeys/register/verify",
            headers={"X-CSRF-Token": csrf},
            json={"credential": {"id": "AAAA"}, "label": "MacBook"},
        )
        assert ok.status_code == 200 and ok.json() == {"ok": True}
        assert "MacBook" in client.get(_REG).text  # the passkey is listed

        # The login page now offers a passkey button.
        client.get("/console/logout")
        assert "Sign in with a passkey" in client.get("/console/login").text

        # Sign in with the passkey (assertion verification mocked).
        client.post("/console/passkeys/login/options")
        monkeypatch.setattr(pk, "verify_authentication", lambda *a, **k: 1)
        signed_in = client.post(
            "/console/passkeys/login/verify", json={"credential": {"id": "AAAA"}}
        )
        assert signed_in.status_code == 200 and signed_in.json() == {"ok": True}
        # The session is now authenticated as the passkey's owner.
        assert client.get("/console", follow_redirects=False).status_code == 200


def test_login_verify_rejects_unknown_passkey(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        client.post("/console/passkeys/login/options")
        resp = client.post(
            "/console/passkeys/login/verify", json={"credential": {"id": "nope"}}
        )
        assert resp.status_code == 400
