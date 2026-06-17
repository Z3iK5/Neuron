# SPDX-License-Identifier: Apache-2.0
"""Durable state for the auditor: the /sync pagination token.

Persisting the ``since`` token means that if the bot restarts, it resumes from
where it left off — the homeserver only re-sends events after that token — so the
audit log has no gaps and no duplicates across restarts.
"""

from __future__ import annotations

import json
from pathlib import Path


class StateStore:
    """A tiny JSON-file store for the auditor's resume token."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def get_since(self) -> str | None:
        """Return the saved sync token, or None on first run."""
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
        except (ValueError, OSError):
            return None
        token = data.get("since")
        return token if isinstance(token, str) else None

    def set_since(self, token: str) -> None:
        """Persist the latest sync token atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"since": token}))
        tmp.replace(self._path)  # atomic on POSIX
