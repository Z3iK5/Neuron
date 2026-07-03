# SPDX-License-Identifier: Apache-2.0
"""Tests for the user directory search and /voip/turnServer endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from pathlib import Path

from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_B = "/_matrix/client/v3"


def _client(tmp_path: Path, **overrides: object) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}", **overrides
    )
    return TestClient(create_app(settings))


def _register(client: TestClient, username: str) -> tuple[str, str]:
    challenge = client.post(_REG, json={"username": username, "password": "pw-123456"})
    session = challenge.json()["session"]
    out = client.post(
        _REG,
        json={
            "username": username,
            "password": "pw-123456",
            "auth": {"type": "m.login.dummy", "session": session},
        },
    ).json()
    return out["access_token"], out["user_id"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _search(client: TestClient, token: str, term: str, **extra: object) -> dict:
    resp = client.post(
        f"{_B}/user_directory/search",
        headers=_h(token),
        json={"search_term": term, **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- user directory ----------------------------------------------------------


def test_directory_matches_localpart_and_displayname(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice_token, alice_id = _register(client, "alice")
        _bob_token, bob_id = _register(client, "bob")
        client.put(
            f"{_B}/profile/{bob_id}/displayname",
            headers=_h(_bob_token),
            json={"displayname": "Robert Tables"},
        )

        by_localpart = _search(client, alice_token, "ali")
        assert [r["user_id"] for r in by_localpart["results"]] == [alice_id]
        assert by_localpart["limited"] is False

        by_displayname = _search(client, alice_token, "TABLES")  # case-insensitive
        assert [r["user_id"] for r in by_displayname["results"]] == [bob_id]
        assert by_displayname["results"][0]["display_name"] == "Robert Tables"

        # The server-name part of the user id must not match.
        assert _search(client, alice_token, "neuron")["results"] == []


def test_directory_escapes_like_metacharacters(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        alice_token, _alice_id = _register(client, "alice")
        bob_token, bob_id = _register(client, "bob")
        client.put(
            f"{_B}/profile/{bob_id}/displayname",
            headers=_h(bob_token),
            json={"displayname": "50% bob"},
        )

        # '%' must match literally, not as a LIKE wildcard: only bob's
        # displayname contains one.
        found = _search(client, alice_token, "0% b")
        assert [r["user_id"] for r in found["results"]] == [bob_id]
        assert _search(client, alice_token, "0%x")["results"] == []


def test_directory_limit_and_deactivated_exclusion(tmp_path: Path) -> None:
    with _client(tmp_path, admin_users="alice") as client:
        alice_token, _ = _register(client, "alice")
        _register(client, "amber")
        _, april_id = _register(client, "april")

        capped = _search(client, alice_token, "a", limit=2)
        assert len(capped["results"]) == 2
        assert capped["limited"] is True

        deactivated = client.post(
            f"/_synapse/admin/v1/deactivate/{april_id}", headers=_h(alice_token), json={}
        )
        assert deactivated.status_code == 200, deactivated.text
        remaining = _search(client, alice_token, "a")
        assert april_id not in {r["user_id"] for r in remaining["results"]}


def test_directory_requires_search_term(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        resp = client.post(f"{_B}/user_directory/search", headers=_h(token), json={})
        assert resp.status_code == 400 and resp.json()["errcode"] == "M_MISSING_PARAM"


# --- /voip/turnServer ---------------------------------------------------------


def test_turn_server_unconfigured_returns_empty(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        token, _ = _register(client, "alice")
        resp = client.get(f"{_B}/voip/turnServer", headers=_h(token))
        assert resp.status_code == 200
        assert resp.json() == {"uris": [], "username": "", "password": "", "ttl": 86400}


def test_turn_server_configured_issues_hmac_credentials(tmp_path: Path) -> None:
    uris = ["turn:turn.example.org:3478?transport=udp", "turns:turn.example.org:5349"]
    with _client(
        tmp_path, turn_uris=uris, turn_shared_secret="s3cret", turn_ttl_s=600
    ) as client:
        token, user_id = _register(client, "alice")
        body = client.get(f"{_B}/voip/turnServer", headers=_h(token)).json()

        assert body["uris"] == uris
        assert body["ttl"] == 600
        expiry_str, _, cred_user = body["username"].partition(":")
        assert cred_user == user_id
        expiry = int(expiry_str)
        assert time.time() < expiry <= time.time() + 600 + 5

        expected = base64.b64encode(
            hmac.new(b"s3cret", body["username"].encode(), hashlib.sha1).digest()
        ).decode()
        assert body["password"] == expected


def test_turn_server_without_secret_is_unconfigured(tmp_path: Path) -> None:
    with _client(tmp_path, turn_uris=["turn:t.example:3478"]) as client:
        token, _ = _register(client, "alice")
        body = client.get(f"{_B}/voip/turnServer", headers=_h(token)).json()
        assert body["uris"] == [] and body["username"] == "" and body["password"] == ""
