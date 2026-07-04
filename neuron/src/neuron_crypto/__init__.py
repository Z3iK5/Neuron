# SPDX-License-Identifier: Apache-2.0
"""neuron_crypto — end-to-end-encryption (E2EE) helpers for Neuron bots.

Matrix encrypts room messages with **Megolm** (`m.megolm.v1.aes-sha2`): each
sender creates an *outbound group session* and shares the matching *inbound
group session key* with the devices it trusts (via Olm-encrypted to-device
`m.room_key` events, or via server-side key backup). A device can only read a
message if it holds the inbound session for that message's ``session_id``.

This package provides:

- ``Decryptor`` / ``DecryptResult`` (in ``base`` — no libolm dependency), so
  services can depend on the *interface* without the E2EE extra.
- ``MegolmSessionStore`` / ``MegolmDecryptor`` (in ``megolm`` — requires libolm
  via the ``e2e`` extra), which actually decrypt Megolm events given the keys.

**Honest limitation (forward-only):** the bot can only decrypt messages whose
inbound session it holds. Keys are shared going forward, so messages sent before
the bot was a trusted member cannot be read unless their keys are imported
(e.g. from a key export / server-side key backup). Undecryptable events are
recorded as envelopes, never silently dropped.
"""

from neuron_crypto.base import Decryptor, DecryptResult

__all__ = ["DecryptResult", "Decryptor"]
