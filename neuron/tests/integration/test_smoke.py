# SPDX-License-Identifier: Apache-2.0
"""Integration smoke test against a LIVE Synapse.

Unlike the unit tests, this talks to a real homeserver. It is automatically
**skipped** unless both ``NEURON_SYNAPSE_BASE_URL`` and
``NEURON_SYNAPSE_ADMIN_TOKEN`` are set in the environment and point at a
reachable, admin-capable Synapse (see ``neuron/deploy/compose/README.md``).

Run it after bringing up the dev stack::

    export NEURON_SYNAPSE_BASE_URL=http://localhost:8008
    export NEURON_SYNAPSE_ADMIN_TOKEN=<server-admin token>
    pytest neuron/tests/integration -q
"""

from __future__ import annotations

import os

import pytest

from neuron_core import SynapseAdminClient

_BASE_URL = os.environ.get("NEURON_SYNAPSE_BASE_URL")
_TOKEN = os.environ.get("NEURON_SYNAPSE_ADMIN_TOKEN")

# Skip the whole module unless we have a homeserver to talk to.
pytestmark = pytest.mark.skipif(
    not (_BASE_URL and _TOKEN),
    reason="Set NEURON_SYNAPSE_BASE_URL and NEURON_SYNAPSE_ADMIN_TOKEN to run integration tests.",
)


async def test_server_version_is_reachable() -> None:
    async with SynapseAdminClient(_BASE_URL, _TOKEN) as admin:  # type: ignore[arg-type]
        version = await admin.get_server_version()
    assert "server_version" in version


async def test_can_list_users() -> None:
    # Phase 0 acceptance criterion: list users via GET /_synapse/admin/v2/users.
    async with SynapseAdminClient(_BASE_URL, _TOKEN) as admin:  # type: ignore[arg-type]
        page = await admin.list_users(limit=10)
    # A freshly bootstrapped server has at least the admin user we created.
    assert page.total >= 1
    assert isinstance(page.users, list)


async def test_can_list_rooms() -> None:
    # Phase 1 acceptance criterion: the console's room listing works end to end.
    async with SynapseAdminClient(_BASE_URL, _TOKEN) as admin:  # type: ignore[arg-type]
        page = await admin.list_rooms(limit=10)
    assert isinstance(page.rooms, list)
    assert page.total_rooms >= 0
