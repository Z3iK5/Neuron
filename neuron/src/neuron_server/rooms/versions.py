# SPDX-License-Identifier: Apache-2.0
"""Room version definitions and the event redaction algorithm.

``neuron_server`` targets **room version 11** for now (the current stable
version). The redaction algorithm — which content keys survive when an event is
redacted — is defined per room version by the Matrix spec; we implement the v11
rules here.

Clean-room: this follows the spec's "Redactions" / room-version grammar, not any
server's source.
"""

from __future__ import annotations

from typing import Any

DEFAULT_ROOM_VERSION = "11"
SUPPORTED_ROOM_VERSIONS = frozenset({"11"})

# Content keys preserved by the redaction algorithm, per event type (room v11).
# Any event type not listed keeps no content keys. The event *envelope*
# (type, sender, room_id, state_key, ...) is always preserved by the caller.
_REDACTION_CONTENT_KEYS: dict[str, frozenset[str]] = {
    "m.room.member": frozenset(
        {"membership", "join_authorised_via_users_server", "third_party_invite"}
    ),
    "m.room.join_rules": frozenset({"join_rule", "allow"}),
    "m.room.power_levels": frozenset(
        {
            "ban",
            "events",
            "events_default",
            "invite",
            "kick",
            "redact",
            "state_default",
            "users",
            "users_default",
        }
    ),
    "m.room.history_visibility": frozenset({"history_visibility"}),
    "m.room.redaction": frozenset({"redacts"}),
}

# In room v11 the whole content of these events is preserved.
_REDACTION_KEEP_ALL_CONTENT = frozenset({"m.room.create"})


def is_supported(room_version: str) -> bool:
    return room_version in SUPPORTED_ROOM_VERSIONS


def redact_content(event_type: str, content: dict[str, Any]) -> dict[str, Any]:
    """Return the content that survives redaction of an event of ``event_type``."""
    if event_type in _REDACTION_KEEP_ALL_CONTENT:
        return dict(content)
    keep = _REDACTION_CONTENT_KEYS.get(event_type, frozenset())
    return {key: content[key] for key in keep if key in content}
