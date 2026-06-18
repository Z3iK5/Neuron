# SPDX-License-Identifier: Apache-2.0
"""The server's federation identity: its Ed25519 signing key and the signed
``/_matrix/key/v2/server`` key-publishing document (HS-7).

This is the first brick of server-to-server federation: every other server
fetches and caches this document to verify our signatures on events and
transactions. The key is loaded from a file (if ``signing_key_path`` is set) or
generated once and persisted in the database so it stays stable across restarts.
"""

from __future__ import annotations

import os
import secrets
import string
import time
from typing import Any

from neuron_core import get_logger
from neuron_server.config import NeuronServerSettings
from neuron_server.crypto.signing import (
    SigningKey,
    generate_signing_key,
    parse_signing_key,
    sign_json,
)
from neuron_server.storage.database import Database
from neuron_server.storage.metadata import get_metadata, set_metadata

_logger = get_logger(__name__)

_METADATA_KEY = "signing_key"
_VERSION_ALPHABET = string.ascii_letters + string.digits


def _new_key_version() -> str:
    """A short random key version, e.g. ``a_x7Kq`` (chars valid in a key id)."""
    return "a_" + "".join(secrets.choice(_VERSION_ALPHABET) for _ in range(6))


class ServerKeyService:
    """Holds the server signing key and builds the published key document."""

    def __init__(
        self, *, server_name: str, signing_key: SigningKey, validity_period_ms: int
    ) -> None:
        self._server_name = server_name
        self._signing_key = signing_key
        self._validity_period_ms = validity_period_ms

    @property
    def signing_key(self) -> SigningKey:
        return self._signing_key

    @classmethod
    async def load_or_create(
        cls, db: Database, settings: NeuronServerSettings
    ) -> ServerKeyService:
        if settings.signing_key_path:
            signing_key = cls._load_or_create_file(settings.signing_key_path)
        else:
            signing_key = await cls._load_or_create_db(db)
        return cls(
            server_name=settings.name,
            signing_key=signing_key,
            validity_period_ms=settings.key_validity_period_ms,
        )

    @staticmethod
    def _load_or_create_file(path: str) -> SigningKey:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return parse_signing_key(handle.read())
        signing_key = generate_signing_key(_new_key_version())
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(signing_key.serialize() + "\n")
        os.chmod(path, 0o600)
        _logger.info("generated new server signing key", extra={"key_id": signing_key.key_id})
        return signing_key

    @staticmethod
    async def _load_or_create_db(db: Database) -> SigningKey:
        stored = await get_metadata(db, _METADATA_KEY)
        if stored:
            return parse_signing_key(stored)
        signing_key = generate_signing_key(_new_key_version())
        await set_metadata(db, _METADATA_KEY, signing_key.serialize())
        _logger.info("generated new server signing key", extra={"key_id": signing_key.key_id})
        return signing_key

    def verify_keys(self) -> dict[str, dict[str, str]]:
        return {self._signing_key.key_id: {"key": self._signing_key.verify_key_base64()}}

    def server_key_document(self) -> dict[str, Any]:
        """The signed ``GET /_matrix/key/v2/server`` response body."""
        document: dict[str, Any] = {
            "server_name": self._server_name,
            "valid_until_ts": int(time.time() * 1000) + self._validity_period_ms,
            "verify_keys": self.verify_keys(),
            "old_verify_keys": {},
        }
        return sign_json(
            document, server_name=self._server_name, signing_key=self._signing_key
        )
