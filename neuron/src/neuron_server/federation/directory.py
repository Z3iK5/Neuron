# SPDX-License-Identifier: Apache-2.0
"""Outbound room-directory federation: resolve a *remote* room alias.

When a local user joins by an alias whose server part is not ours, we ask that
server's ``/_matrix/federation/v1/query/directory`` (X-Matrix signed) to map the
alias to a room id and the servers that can service a join.
"""

from __future__ import annotations

from urllib.parse import quote

from neuron_server.errors import MatrixError
from neuron_server.federation.client import FederationClient
from neuron_server.storage.directory import alias_server


async def resolve_remote_alias(
    client: FederationClient, alias: str
) -> tuple[str, list[str]]:
    """Resolve a remote ``alias`` to ``(room_id, servers)`` over federation."""
    server = alias_server(alias)
    path = f"/_matrix/federation/v1/query/directory?room_alias={quote(alias, safe='')}"
    try:
        response = await client.get_json(server, path)
    except Exception as exc:  # noqa: BLE001 - surface as a spec error to the client
        raise MatrixError(404, "M_NOT_FOUND", f"Could not resolve {alias}") from exc
    room_id = response.get("room_id")
    if not isinstance(room_id, str):
        raise MatrixError(404, "M_NOT_FOUND", "Room alias not found")
    servers = [s for s in response.get("servers", []) if isinstance(s, str)]
    # The queried server is always a candidate even if it omits itself.
    if server not in servers:
        servers.append(server)
    return room_id, servers
