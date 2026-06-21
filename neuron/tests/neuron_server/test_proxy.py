# SPDX-License-Identifier: Apache-2.0
"""Trusted reverse-proxy client-IP / scheme resolution + secure session cookie."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.proxy import ProxyHeadersMiddleware, client_ip, resolve_forwarded


def _scope(peer: str, headers: dict[str, str], *, type: str = "http") -> dict:
    return {
        "type": type,
        "client": (peer, 12345),
        "scheme": "ws" if type == "websocket" else "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }


def _scope_raw_headers(peer: str, raw: list[tuple[bytes, bytes]]) -> dict:
    return {"type": "http", "client": (peer, 12345), "scheme": "http", "headers": raw}


def test_untrusted_peer_keeps_its_own_ip() -> None:
    # A direct client is not a trusted proxy, so its X-Forwarded-For is ignored.
    scope = _scope("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})
    ip, proto = resolve_forwarded(scope, frozenset({"10.0.0.1"}))
    assert ip == "203.0.113.9"
    assert proto is None


def test_trusted_proxy_uses_rightmost_nontrusted_hop() -> None:
    # Chain: real client -> proxyB -> proxyA(=peer). Both proxies are trusted, so
    # the right-most non-trusted entry (the real client) wins; a spoofed left entry
    # is ignored.
    scope = _scope(
        "10.0.0.1",
        {"x-forwarded-for": "9.9.9.9, 198.51.100.7, 10.0.0.2", "x-forwarded-proto": "https"},
    )
    ip, proto = resolve_forwarded(scope, frozenset({"10.0.0.1", "10.0.0.2"}))
    assert ip == "198.51.100.7"
    assert proto == "https"


def test_spoofed_xff_cannot_impersonate_when_single_proxy() -> None:
    # One trusted proxy appends the real client; an attacker-set left entry is to the
    # left of the real client and must not be selected.
    scope = _scope("10.0.0.1", {"x-forwarded-for": "6.6.6.6, 203.0.113.50"})
    ip, _ = resolve_forwarded(scope, frozenset({"10.0.0.1"}))
    assert ip == "203.0.113.50"


def test_wildcard_trusts_chain_and_takes_original_client() -> None:
    scope = _scope("172.16.0.5", {"x-forwarded-for": "203.0.113.7, 172.16.0.9"})
    ip, _ = resolve_forwarded(scope, frozenset({"*"}))
    assert ip == "203.0.113.7"


def test_trusted_proxy_without_xff_falls_back_to_peer() -> None:
    scope = _scope("10.0.0.1", {})
    ip, _ = resolve_forwarded(scope, frozenset({"10.0.0.1"}))
    assert ip == "10.0.0.1"


def test_multiple_xff_header_lines_are_joined_and_walked() -> None:
    # Two proxies each append their hop as a SEPARATE X-Forwarded-For line. The
    # whole chain must be walked, not just the last line, so the right-most
    # non-trusted entry (the real client) is found.
    scope = _scope_raw_headers(
        "10.0.0.1",
        [
            (b"x-forwarded-for", b"203.0.113.5"),  # real client (added by proxyB)
            (b"x-forwarded-for", b"10.0.0.2"),  # proxyB (added by proxyA, the peer)
        ],
    )
    ip, _ = resolve_forwarded(scope, frozenset({"10.0.0.1", "10.0.0.2"}))
    assert ip == "203.0.113.5"


def test_ipv4_port_and_ipv6_brackets_are_stripped() -> None:
    scope = _scope("10.0.0.1", {"x-forwarded-for": "[2001:db8::9]:443"})
    ip, _ = resolve_forwarded(scope, frozenset({"10.0.0.1"}))
    assert ip == "2001:db8::9"
    scope = _scope("10.0.0.1", {"x-forwarded-for": "203.0.113.5:51789"})
    ip, _ = resolve_forwarded(scope, frozenset({"10.0.0.1"}))
    assert ip == "203.0.113.5"


def test_bare_ipv6_client_is_preserved() -> None:
    scope = _scope("10.0.0.1", {"x-forwarded-for": "2001:db8::abcd"})
    ip, _ = resolve_forwarded(scope, frozenset({"10.0.0.1"}))
    assert ip == "2001:db8::abcd"


def test_websocket_scheme_maps_to_wss_not_https() -> None:
    scope = _scope(
        "10.0.0.1", {"x-forwarded-proto": "https"}, type="websocket"
    )
    # resolve_forwarded reports the raw proto; the middleware does the ws/wss mapping.
    _, proto = resolve_forwarded(scope, frozenset({"10.0.0.1"}))
    assert proto == "https"


def test_middleware_rewrites_client_and_scheme() -> None:
    seen: dict[str, str] = {}
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        seen["ip"] = client_ip(request)
        seen["scheme"] = request.url.scheme
        return seen

    app.add_middleware(ProxyHeadersMiddleware, trusted=frozenset({"*"}))
    with TestClient(app) as client:
        client.get(
            "/whoami",
            headers={"x-forwarded-for": "203.0.113.77", "x-forwarded-proto": "https"},
        )
    assert seen["ip"] == "203.0.113.77"
    assert seen["scheme"] == "https"


async def test_middleware_maps_websocket_scheme_to_wss() -> None:
    captured: dict = {}

    async def app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        captured.update(scope)

    mw = ProxyHeadersMiddleware(app, trusted=frozenset({"10.0.0.1"}))
    scope = _scope("10.0.0.1", {"x-forwarded-proto": "https"}, type="websocket")
    await mw(scope, None, None)  # type: ignore[arg-type]
    assert captured["scheme"] == "wss"  # not "https"
    assert captured["client"][0] == "10.0.0.1"


def _settings(tmp_path: Path, **over: object) -> NeuronServerSettings:
    return NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        public_base_url="http://localhost:8008",
        **over,
    )


def _login_set_cookie(client: TestClient) -> str:
    """Run the console login flow and return the raw Set-Cookie for the session."""
    client.post("/get-started", data={"username": "founder", "password": "s3cret-password"})
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/console/login").text)
    assert token
    resp = client.post(
        "/console/login",
        data={"username": "founder", "password": "s3cret-password", "csrf_token": token.group(1)},
        follow_redirects=False,
    )
    return " ".join(resp.headers.get_list("set-cookie"))


def test_session_cookie_secure_flag_off_by_default(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path, first_user_admin=True))) as client:
        cookies = _login_set_cookie(client)
        assert "neuron_session=" in cookies
        assert "secure" not in cookies.lower()


def test_session_cookie_secure_flag_on_when_https_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path, first_user_admin=True, session_https_only=True)
    with TestClient(create_app(settings)) as client:
        cookies = _login_set_cookie(client)
        assert "neuron_session=" in cookies
        assert "secure" in cookies.lower()


def test_proxy_middleware_installed_only_when_trusted(tmp_path: Path) -> None:
    plain = create_app(_settings(tmp_path))
    assert not any(m.cls is ProxyHeadersMiddleware for m in plain.user_middleware)
    proxied = create_app(_settings(tmp_path, trusted_proxies="10.0.0.1"))
    assert any(m.cls is ProxyHeadersMiddleware for m in proxied.user_middleware)
