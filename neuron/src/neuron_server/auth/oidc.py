# SPDX-License-Identifier: Apache-2.0
"""OIDC / MSC3861 delegated authentication (OAuth2 resource server).

When ``oidc_enabled`` is set, Neuron stops handling passwords itself and instead
validates bearer tokens against an external OIDC provider (MAS / Keycloak / Dex)
using RFC 7662 token introspection. This module provides:

- **client discovery** — the provider's OAuth2/OIDC metadata (fetched from the
  issuer's ``/.well-known/openid-configuration`` and cached ~1h), served to
  clients via the MSC2965 ``auth_metadata`` / ``auth_issuer`` endpoints;
- **token validation** — :meth:`OidcAuth.validate` introspects a bearer token,
  maps an active subject to a (provisioned-on-first-sight) local user, and caches
  the active result briefly keyed by a hash of the token (the token is never
  logged or used as a cache key directly).

The HTTP client is injectable (the ``open_client`` seam, mirroring
:class:`~neuron_server.federation.client.FederationClient`) so tests can serve
discovery + introspection in-process without touching the network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import httpx

from neuron_server.auth.service import Authenticated
from neuron_server.clock import now_ms
from neuron_server.config import NeuronServerSettings
from neuron_server.storage import accounts
from neuron_server.storage.database import Database

OpenClient = Callable[[], httpx.AsyncClient]

# In-process cache horizons: discovery metadata is stable, introspection is not.
_METADATA_TTL_MS = 3_600_000  # ~1h
_INTROSPECTION_TTL_MS = 30_000  # brief — an active token can be revoked upstream


class OidcAuth:
    """Validates bearer tokens against an external OIDC provider by introspection."""

    def __init__(
        self,
        db: Database,
        server_name: str,
        settings: NeuronServerSettings,
        *,
        open_client: OpenClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._db = db
        self._server_name = server_name
        self._issuer = settings.oidc_issuer.rstrip("/")
        self._introspection_endpoint = settings.oidc_introspection_endpoint
        self._client_id = settings.oidc_client_id
        self._client_secret = settings.oidc_client_secret.get_secret_value()
        self._account_management_url = settings.oidc_account_management_url
        self._timeout = timeout
        self.open_client: OpenClient = open_client or self._default_open
        # Cached provider metadata: (fetched_at_ms, document).
        self._metadata: tuple[int, dict[str, Any]] | None = None
        # token_sha256 -> (expiry_ms, Authenticated) for active introspections only.
        self._token_cache: dict[str, tuple[int, Authenticated]] = {}

    def _default_open(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout)

    async def issuer(self) -> str:
        return self._issuer

    async def _openid_configuration(self) -> dict[str, Any]:
        """The provider's OpenID configuration document (cached ~1h)."""
        if self._metadata is not None and now_ms() - self._metadata[0] < _METADATA_TTL_MS:
            return self._metadata[1]
        url = f"{self._issuer}/.well-known/openid-configuration"
        client = self.open_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
            document = response.json()
        finally:
            await client.aclose()
        if not isinstance(document, dict):
            document = {}
        self._metadata = (now_ms(), document)
        return document

    async def auth_metadata(self) -> dict[str, Any]:
        """The provider metadata served to clients (MSC2965), with our additions."""
        metadata = dict(await self._openid_configuration())
        if self._account_management_url:
            metadata["account_management_uri"] = self._account_management_url
        return metadata

    async def _resolve_introspection_endpoint(self) -> str:
        if self._introspection_endpoint:
            return self._introspection_endpoint
        endpoint = (await self._openid_configuration()).get("introspection_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise RuntimeError("OIDC provider exposes no introspection_endpoint")
        return endpoint

    async def validate(self, token: str) -> Authenticated | None:
        """Introspect ``token``; return the identity if active, else ``None``.

        Active results are cached briefly, keyed by a SHA-256 of the token (the
        raw token is never stored as a key nor logged).
        """
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._token_cache.get(token_hash)
        if cached is not None and now_ms() < cached[0]:
            return cached[1]

        endpoint = await self._resolve_introspection_endpoint()
        client = self.open_client()
        try:
            response = await client.post(
                endpoint,
                data={"token": token},
                auth=(self._client_id, self._client_secret),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError:
            return None
        finally:
            await client.aclose()

        if not isinstance(data, dict) or data.get("active") is not True:
            return None

        identity = await self._map_subject(data)
        if identity is None:
            return None
        self._token_cache[token_hash] = (now_ms() + _INTROSPECTION_TTL_MS, identity)
        return identity

    async def _map_subject(self, claims: dict[str, Any]) -> Authenticated | None:
        """Map an active introspection response to a (provisioned) local user."""
        subject = (
            claims.get("username")
            or claims.get("preferred_username")
            or claims.get("sub")
        )
        if not isinstance(subject, str) or not subject:
            return None
        user_id = subject if subject.startswith("@") else f"@{subject}:{self._server_name}"

        # Provision the account row on first sight (no password — auth is external).
        if not await accounts.user_exists(self._db, user_id):
            await accounts.create_user(self._db, user_id, None, False, now_ms())

        device = claims.get("device_id")
        device_id = (
            device
            if isinstance(device, str) and device
            else "oidc_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
        )
        return Authenticated(user_id=user_id, device_id=device_id)
