# SPDX-License-Identifier: Apache-2.0
"""E2EE decryption interface — the olm-free part.

This module deliberately has **no dependency on libolm**, so services can import
the ``Decryptor`` protocol and the ``DecryptResult`` type (and use the plaintext
``NullDecryptor``) without the E2EE extra installed. The actual megolm
implementation lives in ``neuron_crypto.megolm`` and is imported only when E2EE
is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class DecryptResult:
    """Outcome of attempting to decrypt one encrypted event.

    On success, ``event_type`` and ``content`` hold the *inner* (cleartext) event.
    On failure, ``reason`` explains why (e.g. the key wasn't available) so the
    auditor can record an honest "could not decrypt" envelope rather than drop it.
    """

    decrypted: bool
    event_type: str | None = None
    content: dict[str, Any] | None = None
    reason: str | None = None
    sender_curve25519: str | None = None


class Decryptor(Protocol):
    """Anything that can attempt to decrypt an ``m.room.encrypted`` event."""

    def decrypt(self, event: dict[str, Any]) -> DecryptResult: ...


class NullDecryptor:
    """A decryptor that never decrypts — used when E2EE support is disabled.

    Encrypted events are reported as undecryptable (so they are recorded as
    envelopes, not dropped), exactly matching the plaintext-only behavior.
    """

    def decrypt(self, event: dict[str, Any]) -> DecryptResult:
        return DecryptResult(decrypted=False, reason="end-to-end encryption is not enabled")
