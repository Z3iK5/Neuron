"""Megolm decryption: an inbound group-session store + a Decryptor.

Requires libolm (installed via the ``e2e`` extra: ``pip install -e ".[e2e]"``).

The store holds inbound Megolm sessions keyed by ``session_id``. You populate it
by importing session keys — from ``m.room_key`` events the bot receives, from a
key-export file, or from server-side key backup — and then ``decrypt`` an
``m.room.encrypted`` event by looking up its session and decrypting the Megolm
ciphertext. Sessions can be persisted (pickled) so a restart keeps its keys,
mirroring how Element persists a device's keys.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import olm

from neuron_crypto.base import DecryptResult

MEGOLM_ALGORITHM = "m.megolm.v1.aes-sha2"
ROOM_KEY_TYPE = "m.room_key"


class MegolmSessionStore:
    """Holds inbound Megolm group sessions and decrypts events against them."""

    def __init__(self, *, pickle_key: str = "neuron") -> None:
        self._sessions: dict[str, olm.InboundGroupSession] = {}
        self._pickle_key = pickle_key

    # --- importing keys -----------------------------------------------------
    def import_session_key(self, session_key: str) -> str:
        """Create an inbound session from a Megolm session key. Returns its id.

        ``session_key`` is the base64 value found in an ``m.room_key`` event's
        ``session_key`` field (or exported from a client).
        """
        session = olm.InboundGroupSession(session_key)
        self._sessions[session.id] = session
        return session.id

    def import_room_key(self, content: dict[str, Any]) -> str | None:
        """Import the key from an ``m.room_key`` event's content (if Megolm).

        Returns the session id imported, or ``None`` if the content isn't a
        Megolm room key.
        """
        if content.get("algorithm") != MEGOLM_ALGORITHM:
            return None
        session_key = content.get("session_key")
        if not isinstance(session_key, str):
            return None
        return self.import_session_key(session_key)

    def import_key_file(self, path: str) -> int:
        """Import sessions from a simple JSON key file. Returns the count imported.

        The file is a JSON array of objects each carrying at least a
        ``session_key`` (and optionally the rest of an ``m.room_key`` content).
        Such a file is sensitive (it can decrypt messages) and must be protected.
        """
        entries = json.loads(Path(path).read_text())
        count = 0
        for entry in entries:
            key = entry.get("session_key")
            if isinstance(key, str):
                self.import_session_key(key)
                count += 1
        return count

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    # --- decryption ---------------------------------------------------------
    def decrypt_event(self, event: dict[str, Any]) -> DecryptResult:
        """Attempt to decrypt one ``m.room.encrypted`` event."""
        content = event.get("content", {}) or {}
        if content.get("algorithm") != MEGOLM_ALGORITHM:
            return DecryptResult(decrypted=False, reason="not a Megolm-encrypted event")

        session_id = content.get("session_id")
        ciphertext = content.get("ciphertext")
        if not isinstance(session_id, str) or not isinstance(ciphertext, str):
            return DecryptResult(decrypted=False, reason="malformed encrypted event")

        session = self._sessions.get(session_id)
        if session is None:
            return DecryptResult(
                decrypted=False, reason="no session (decryption key not available)"
            )

        try:
            plaintext, _index = session.decrypt(ciphertext)
        except olm.OlmGroupSessionError as exc:
            return DecryptResult(decrypted=False, reason=f"decryption failed: {exc}")

        try:
            payload = json.loads(plaintext)
        except ValueError:
            return DecryptResult(decrypted=False, reason="decrypted payload was not JSON")

        return DecryptResult(
            decrypted=True,
            event_type=payload.get("type"),
            content=payload.get("content"),
            sender_curve25519=content.get("sender_key"),
        )

    # --- persistence --------------------------------------------------------
    def save(self, path: str) -> None:
        """Persist all inbound sessions (pickled) to a JSON file."""
        data = {
            sid: base64.b64encode(session.pickle(self._pickle_key)).decode("ascii")
            for sid, session in self._sessions.items()
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data))

    def load(self, path: str) -> int:
        """Load inbound sessions previously written by :meth:`save`. Returns count."""
        p = Path(path)
        if not p.exists():
            return 0
        data = json.loads(p.read_text())
        for sid, pickled_b64 in data.items():
            self._sessions[sid] = olm.InboundGroupSession.from_pickle(
                base64.b64decode(pickled_b64), self._pickle_key
            )
        return len(self._sessions)


class MegolmDecryptor:
    """A ``Decryptor`` backed by a :class:`MegolmSessionStore`."""

    def __init__(self, store: MegolmSessionStore) -> None:
        self._store = store

    def decrypt(self, event: dict[str, Any]) -> DecryptResult:
        return self._store.decrypt_event(event)
