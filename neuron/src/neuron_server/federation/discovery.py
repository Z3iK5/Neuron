# SPDX-License-Identifier: Apache-2.0
"""Matrix server-name resolution (where to actually connect for a server) — HS-7.

Implements the spec's server discovery short of SRV records: an explicit port in
the server name wins; otherwise the server's ``/.well-known/matrix/server``
delegation is honoured; otherwise we fall back to port 8448. The network fetch is
kept separate from the pure decision so the latter can be unit-tested.
"""

from __future__ import annotations

from typing import Any

_DEFAULT_PORT = 8448


def pick_base_url(server_name: str, well_known: dict[str, Any] | None) -> str:
    """Decide the ``https://host:port`` base URL for ``server_name``.

    ``well_known`` is the parsed ``/.well-known/matrix/server`` document (or
    ``None`` if absent/unreachable).
    """
    if ":" in server_name:
        # Explicit port: connect directly, no delegation.
        return f"https://{server_name}"
    if well_known:
        delegated = well_known.get("m.server")
        if isinstance(delegated, str) and delegated:
            if ":" in delegated:
                return f"https://{delegated}"
            return f"https://{delegated}:{_DEFAULT_PORT}"
    return f"https://{server_name}:{_DEFAULT_PORT}"
