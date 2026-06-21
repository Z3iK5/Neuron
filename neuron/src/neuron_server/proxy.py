# SPDX-License-Identifier: Apache-2.0
"""Trusted-proxy client-IP and scheme resolution.

When Neuron runs behind a reverse proxy (nginx, Caddy, a load balancer), the TCP
peer is the proxy — so ``request.client.host`` is the proxy's address and the
user's real IP arrives in the ``X-Forwarded-For`` header (and the original scheme
in ``X-Forwarded-Proto``). Honouring those headers blindly would let any client
spoof its IP, so :class:`ProxyHeadersMiddleware`:

- only trusts the headers when the immediate peer is one of the configured
  ``trusted_proxies`` (or when ``"*"`` trusts any peer); and
- resolves the client as the **right-most** ``X-Forwarded-For`` entry that is not
  itself a trusted proxy. ``X-Forwarded-For`` reads left-to-right as
  ``original-client, proxy1, proxy2`` with each hop appending the address it saw,
  so walking from the right past our own trusted hops lands on the true client.
  Entries an attacker injects sit to the *left* of the real client and are ignored.

When no proxies are trusted the middleware isn't installed and the raw TCP peer is
used unchanged — correct for a directly-exposed or desktop server. Route code
should read the resolved address via :func:`client_ip` rather than touching
``request.client`` directly, so there is a single, documented trust boundary.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send


def client_ip(request: Request) -> str:
    """The resolved client IP (proxy-aware once the middleware has run)."""
    return request.client.host if request.client else "unknown"


def _join_header(scope: Scope, name: bytes) -> str:
    """All values of a (possibly repeated) header, comma-joined in arrival order.

    A proxy may append its hop as a *separate* ``X-Forwarded-For`` line rather than
    extending the existing one, so we concatenate every line before walking the
    chain — picking only the last line would drop earlier proxy hops.
    """
    parts = [val.decode("latin-1") for key, val in scope.get("headers", []) if key == name]
    return ", ".join(p for p in parts if p.strip())


def _clean_hop(hop: str) -> str:
    """Strip brackets / a trailing port from one forwarded address.

    Handles ``ipv4:port`` and ``[ipv6]``/``[ipv6]:port`` while leaving a bare IPv6
    (which contains colons but no port) untouched.
    """
    hop = hop.strip()
    if hop.startswith("[") and "]" in hop:
        return hop[1 : hop.index("]")]
    if hop.count(":") == 1:  # ipv4:port (a bare IPv6 has several colons)
        return hop.split(":", 1)[0]
    return hop


def resolve_forwarded(scope: Scope, trusted: frozenset[str]) -> tuple[str | None, str | None]:
    """Resolve ``(client_ip, scheme)`` from X-Forwarded-* honouring ``trusted``.

    Returns the unchanged peer IP and ``None`` scheme when the immediate peer is
    not a trusted proxy (so untrusted clients can never spoof either value).
    """
    client = scope.get("client")
    peer = client[0] if client else None
    if peer is None:
        return None, None
    trust_any = "*" in trusted
    if not trust_any and peer not in trusted:
        return peer, None  # direct, untrusted peer: ignore any forwarded headers

    resolved = peer
    hops = [_clean_hop(h) for h in _join_header(scope, b"x-forwarded-for").split(",") if h.strip()]
    if hops:
        if trust_any:
            # No proxy list to walk; trust the chain and take the original client.
            resolved = hops[0]
        else:
            # Right-most hop that isn't one of our proxies is the real client.
            resolved = next((h for h in reversed(hops) if h not in trusted), hops[0])

    proto = _join_header(scope, b"x-forwarded-proto").split(",")[0].strip().lower() or None
    return resolved, proto


class ProxyHeadersMiddleware:
    """Rewrite ``scope['client']`` / ``scope['scheme']`` from trusted X-Forwarded-*."""

    def __init__(self, app: ASGIApp, trusted: frozenset[str]) -> None:
        self.app = app
        self.trusted = trusted

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            ip, proto = resolve_forwarded(scope, self.trusted)
            if ip is not None:
                # Preserve the original port slot; only the host is meaningful here.
                port = scope["client"][1] if scope.get("client") else 0
                scope = dict(scope)
                scope["client"] = (ip, port)
                if proto in ("http", "https"):
                    # Map to the right scheme vocabulary: ws/wss for a websocket,
                    # http/https otherwise (don't clobber wss -> https).
                    if scope["type"] == "websocket":
                        scope["scheme"] = "wss" if proto == "https" else "ws"
                    else:
                        scope["scheme"] = proto
        await self.app(scope, receive, send)
