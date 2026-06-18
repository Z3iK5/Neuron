# SPDX-License-Identifier: Apache-2.0
"""Resolving the Ed25519 verify keys of any server, local or remote (HS-7).

For our own server name we return our published keys directly. For a remote
server we return cached keys if still valid, otherwise fetch its
``/_matrix/key/v2/server`` document, verify it is correctly self-signed, cache
it, and return the keys. This is what lets us authenticate inbound federation
requests from real remote servers.
"""

from __future__ import annotations

import time
from typing import Any

from neuron_core import get_logger
from neuron_server.crypto.signing import verify_signed_json
from neuron_server.federation.client import FederationClient
from neuron_server.keys.service import ServerKeyService
from neuron_server.storage import federation as fed_store
from neuron_server.storage.database import Database

_logger = get_logger(__name__)


def parse_and_verify_key_document(
    doc: dict[str, Any], expected_server_name: str
) -> dict[str, str] | None:
    """Return ``{key_id: verify_key}`` if ``doc`` is a valid self-signed key
    document for ``expected_server_name``, else ``None``.

    Every advertised verify key must sign the document (the spec's
    self-certification), so a forged or mismatched document is rejected.
    """
    if doc.get("server_name") != expected_server_name:
        return None
    raw_keys = doc.get("verify_keys")
    if not isinstance(raw_keys, dict) or not raw_keys:
        return None
    verify_keys: dict[str, str] = {}
    for key_id, entry in raw_keys.items():
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str):
            return None
        if not verify_signed_json(
            doc, server_name=expected_server_name, verify_key_base64=key, key_id=key_id
        ):
            return None
        verify_keys[key_id] = key
    return verify_keys


class ServerKeyResolver:
    """Resolves verify keys for any server, caching remote keys in the database."""

    def __init__(
        self,
        db: Database,
        our_name: str,
        server_keys: ServerKeyService,
        client: FederationClient,
    ) -> None:
        self._db = db
        self._our_name = our_name
        self._server_keys = server_keys
        self._client = client

    async def verify_keys_for(self, server_name: str) -> dict[str, str]:
        """``{key_id: verify_key}`` for ``server_name``; empty if unresolvable."""
        if server_name == self._our_name:
            return {kid: v["key"] for kid, v in self._server_keys.verify_keys().items()}

        now = int(time.time() * 1000)
        cached = await fed_store.get_cached_server_keys(self._db, server_name, now)
        if cached:
            return cached
        return await self._fetch(server_name, now)

    async def _fetch(self, server_name: str, now: int) -> dict[str, str]:
        try:
            doc = await self._client.get_json(
                server_name, "/_matrix/key/v2/server", sign=False
            )
        except Exception as exc:  # network / HTTP errors are not fatal to us
            _logger.warning("failed to fetch keys for %s: %s", server_name, exc)
            return {}

        verify_keys = parse_and_verify_key_document(doc, server_name)
        if verify_keys is None:
            _logger.warning("rejected invalid key document from %s", server_name)
            return {}

        valid_until = int(doc.get("valid_until_ts", 0))
        if valid_until > now:
            await fed_store.cache_server_keys(self._db, server_name, verify_keys, valid_until)
        return verify_keys
