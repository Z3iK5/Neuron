# SPDX-License-Identifier: Apache-2.0
"""OIDC / MSC3861 delegated auth: off-by-default behaviour, MSC2965 discovery,
and token validation by introspection with an injected in-process HTTP client."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_ISSUER = "https://provider.example"
_INTROSPECT = f"{_ISSUER}/introspect"
_REG = "/_matrix/client/v3/register"
_LOGIN = "/_matrix/client/v3/login"
_REFRESH = "/_matrix/client/v3/refresh"
_WHOAMI = "/_matrix/client/v3/account/whoami"
_AUTH_META = "/_matrix/client/unstable/org.matrix.msc2965/auth_metadata"
_AUTH_ISSUER = "/_matrix/client/unstable/org.matrix.msc2965/auth_issuer"

_DISCOVERY = {
    "issuer": _ISSUER,
    "authorization_endpoint": f"{_ISSUER}/authorize",
    "token_endpoint": f"{_ISSUER}/token",
    "introspection_endpoint": _INTROSPECT,
    "registration_endpoint": f"{_ISSUER}/register",
    "revocation_endpoint": f"{_ISSUER}/revoke",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
}


def _fake_open_client(counter: dict[str, int], active: dict[str, str]):
    """Build an ``open_client`` seam serving discovery + introspection in-process.

    ``active`` maps a bearer token -> its username claim; anything else is inactive.
    ``counter`` records how many introspection POSTs were made (to prove caching).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=_DISCOVERY)
        if str(request.url) == _INTROSPECT:
            counter["introspect"] += 1
            fields = parse_qs(request.content.decode())
            token = fields.get("token", [""])[0]
            if token in active:
                return httpx.Response(200, json={"active": True, "username": active[token]})
            return httpx.Response(200, json={"active": False})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def open_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    return open_client


def _oidc_client(
    tmp_path: Path,
    counter: dict[str, int],
    active: dict[str, str],
    **overrides: Any,
) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        oidc_enabled=True,
        oidc_issuer=_ISSUER,
        oidc_client_id="neuron",
        oidc_client_secret="s3cret",
        oidc_account_management_url="https://provider.example/account",
        **overrides,
    )
    client = TestClient(create_app(settings))
    client.__enter__()  # start the lifespan so app.state.oidc exists
    client.app.state.oidc.open_client = _fake_open_client(counter, active)
    return client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- off by default --------------------------------------------------------


def test_oidc_disabled_is_todays_behaviour(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        registration_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        # Local login flow is advertised, discovery endpoints are absent.
        assert client.get(_LOGIN).status_code == 200
        assert client.get(_AUTH_META).status_code == 404
        assert client.get(_AUTH_ISSUER).status_code == 404
        # Registration still works.
        challenge = client.post(_REG, json={"username": "alice", "password": "pw-123456"})
        session = challenge.json()["session"]
        reg = client.post(
            _REG,
            json={
                "username": "alice",
                "password": "pw-123456",
                "auth": {"type": "m.login.dummy", "session": session},
            },
        )
        assert reg.status_code == 200


# --- enabled ---------------------------------------------------------------


def test_discovery_endpoints_served(tmp_path: Path) -> None:
    counter = {"introspect": 0}
    client = _oidc_client(tmp_path, counter, {})
    try:
        meta = client.get(_AUTH_META)
        assert meta.status_code == 200
        body = meta.json()
        assert body["issuer"] == _ISSUER
        assert body["authorization_endpoint"] == f"{_ISSUER}/authorize"
        assert body["account_management_uri"] == "https://provider.example/account"

        issuer = client.get(_AUTH_ISSUER)
        assert issuer.status_code == 200 and issuer.json() == {"issuer": _ISSUER}
    finally:
        client.__exit__(None, None, None)


def test_introspected_token_provisions_and_authenticates(tmp_path: Path) -> None:
    counter = {"introspect": 0}
    client = _oidc_client(tmp_path, counter, {"tok-abc": "extuser"})
    try:
        resp = client.get(_WHOAMI, headers=_auth("tok-abc"))
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "@extuser:neuron.local"
    finally:
        client.__exit__(None, None, None)


def test_inactive_token_is_rejected(tmp_path: Path) -> None:
    counter = {"introspect": 0}
    client = _oidc_client(tmp_path, counter, {"tok-abc": "extuser"})
    try:
        resp = client.get(_WHOAMI, headers=_auth("bogus"))
        assert resp.status_code == 401 and resp.json()["errcode"] == "M_UNKNOWN_TOKEN"
    finally:
        client.__exit__(None, None, None)


def test_introspection_result_is_cached(tmp_path: Path) -> None:
    counter = {"introspect": 0}
    client = _oidc_client(tmp_path, counter, {"tok-abc": "extuser"})
    try:
        assert client.get(_WHOAMI, headers=_auth("tok-abc")).status_code == 200
        assert client.get(_WHOAMI, headers=_auth("tok-abc")).status_code == 200
        # Two authenticated requests, a single upstream introspection call.
        assert counter["introspect"] == 1
    finally:
        client.__exit__(None, None, None)


def test_local_auth_endpoints_unrecognized_under_oidc(tmp_path: Path) -> None:
    counter = {"introspect": 0}
    client = _oidc_client(tmp_path, counter, {})
    try:
        for resp in (
            client.post(_LOGIN, json={"type": "m.login.password"}),
            client.get(_LOGIN),
            client.post(_REG, json={"username": "x", "password": "y"}),
            client.post(_REFRESH, json={"refresh_token": "x"}),
            client.post("/_matrix/client/v3/account/password", json={}),
            client.post("/_matrix/client/v3/account/deactivate", json={}),
        ):
            assert resp.status_code == 404, resp.request.url
            assert resp.json()["errcode"] == "M_UNRECOGNIZED"
    finally:
        client.__exit__(None, None, None)
