# SPDX-License-Identifier: Apache-2.0
"""Tests for console passkey (WebAuthn) login.

The browser ceremony and the cryptographic attestation/assertion checks belong to
``py_webauthn`` and a real authenticator, so here we test the store, the options
endpoints (which need no authenticator), and the route wiring with the verify step
mocked.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from neuron_console import passkeys as pk
from neuron_console.app import create_app
from neuron_console.config import ConsoleSettings

_PW = "console-pw"


@contextmanager
def _client(tmp_path: Path) -> Iterator[TestClient]:
    settings = ConsoleSettings(
        _env_file=None,
        console_password=SecretStr(_PW),
        console_session_secret=SecretStr("unit-test-secret"),
        homeserver_admin_token=SecretStr("admin-token"),
        server_name="neuron.local",
        console_data_dir=str(tmp_path),
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _login(client: TestClient) -> None:
    assert client.post("/login", data={"password": _PW}, follow_redirects=False).status_code == 303


def _csrf(client: TestClient) -> str:
    match = re.search(r'NEURON_CSRF = "([^"]+)"', client.get("/passkeys").text)
    assert match, "csrf token not found on /passkeys"
    return match.group(1)


def test_passkey_store_roundtrip(tmp_path: Path) -> None:
    store = pk.PasskeyStore(tmp_path / "pk.json")
    assert store.list() == [] and not store.has_any()

    cred = pk.StoredCredential(id="AAAA", public_key="UEs", sign_count=0, label="key", created_ts=1)
    store.add(cred)
    assert store.has_any()
    assert store.get("AAAA") == cred

    store.update_sign_count("AAAA", 7)
    got = store.get("AAAA")
    assert got is not None and got.sign_count == 7

    store.remove("AAAA")
    assert store.list() == [] and store.get("AAAA") is None


def test_register_requires_login(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post(
            "/passkeys/register/options", headers={"X-CSRF-Token": "x"}, follow_redirects=False
        )
        assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_register_options_needs_csrf(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _csrf(client)  # establishes a session csrf token
        # Wrong/missing token is rejected (CSRF error -> 400 error page).
        resp = client.post("/passkeys/register/options", headers={"X-CSRF-Token": "wrong"})
        assert resp.status_code == 400


def test_register_then_appears_and_can_be_removed(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        _login(client)
        csrf = _csrf(client)

        opts = client.post("/passkeys/register/options", headers={"X-CSRF-Token": csrf})
        assert opts.status_code == 200 and "challenge" in opts.json()

        # The attestation check is py_webauthn's job; mock it to return a credential.
        cred = pk.StoredCredential(
            id="AAAABBBB", public_key="PUBKEY", sign_count=0, label="My Key", created_ts=1
        )
        monkeypatch.setattr(pk, "verify_registration", lambda *a, **k: cred)
        verify = client.post(
            "/passkeys/register/verify",
            json={"credential": {"id": "AAAABBBB"}, "label": "My Key"},
            headers={"X-CSRF-Token": csrf},
        )
        assert verify.status_code == 200 and verify.json() == {"ok": True}

        page = client.get("/passkeys").text
        assert "My Key" in page and "AAAABBBB" in page

        # Remove it via the management form.
        removed = client.post(
            "/passkeys/delete",
            data={"csrf_token": csrf, "credential_id": "AAAABBBB"},
            follow_redirects=False,
        )
        assert removed.status_code == 303
        assert not pk.PasskeyStore(tmp_path / "passkeys.json").has_any()


def test_passkey_login_authenticates_the_session(tmp_path: Path, monkeypatch) -> None:
    # Seed a registered credential.
    pk.PasskeyStore(tmp_path / "passkeys.json").add(
        pk.StoredCredential(
            id="AAAABBBB", public_key="PUBKEY", sign_count=0, label="k", created_ts=1
        )
    )
    with _client(tmp_path) as client:
        # Not logged in yet: the dashboard redirects to login.
        assert client.get("/passkeys", follow_redirects=False).status_code == 303

        opts = client.post("/passkeys/login/options")
        assert opts.status_code == 200 and "challenge" in opts.json()

        monkeypatch.setattr(pk, "verify_authentication", lambda *a, **k: "AAAABBBB")
        verify = client.post("/passkeys/login/verify", json={"credential": {"id": "AAAABBBB"}})
        assert verify.status_code == 200 and verify.json() == {"ok": True}

        # The session is now authenticated.
        assert client.get("/passkeys", follow_redirects=False).status_code == 200


def test_passkey_login_failure_does_not_authenticate(tmp_path: Path, monkeypatch) -> None:
    pk.PasskeyStore(tmp_path / "passkeys.json").add(
        pk.StoredCredential(
            id="AAAABBBB", public_key="PUBKEY", sign_count=0, label="k", created_ts=1
        )
    )
    with _client(tmp_path) as client:
        client.post("/passkeys/login/options")

        def _boom(*_a, **_k):
            raise ValueError("bad assertion")

        monkeypatch.setattr(pk, "verify_authentication", _boom)
        verify = client.post("/passkeys/login/verify", json={"credential": {"id": "AAAABBBB"}})
        assert verify.status_code == 400
        # Still not authenticated.
        assert client.get("/passkeys", follow_redirects=False).status_code == 303
