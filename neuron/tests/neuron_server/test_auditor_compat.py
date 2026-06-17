# SPDX-License-Identifier: Apache-2.0
"""HS-3 done-criterion: the real neuron_auditor syncs against neuron_server.

Runs the actual ``neuron_auditor.Auditor`` (which drives ``MatrixClient.sync``)
against an in-process ``neuron_server``. A bot creates a room and sends a message;
the auditor's sync cycle must record that message — proving live events flow
through ``/sync`` to a Neuron service.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from neuron_auditor.core import Auditor
from neuron_auditor.state import StateStore
from neuron_core import MatrixClient
from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

_REG = "/_matrix/client/v3/register"
_B = "/_matrix/client/v3"


class _RecordingSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


async def test_auditor_records_messages_from_neuron_server(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    app = create_app(settings)
    base = "http://hs.test"

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base) as raw:
            challenge = await raw.post(_REG, json={"username": "auditbot", "password": "pw-123456"})
            session = challenge.json()["session"]
            reg = await raw.post(
                _REG,
                json={
                    "username": "auditbot",
                    "password": "pw-123456",
                    "auth": {"type": "m.login.dummy", "session": session},
                },
            )
            token = reg.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            room = (await raw.post(f"{_B}/createRoom", headers=headers, json={})).json()["room_id"]
            await raw.put(
                f"{_B}/rooms/{room}/send/m.room.message/m1",
                headers=headers,
                json={"msgtype": "m.text", "body": "audit me"},
            )

        # Drive the real Auditor against the same app with neuron_core's client.
        core_client = httpx.AsyncClient(transport=transport, base_url=base, headers=headers)
        client = MatrixClient(base, token, client=core_client)
        auditor = Auditor(
            client,
            _RecordingSink(),  # type: ignore[arg-type]
            StateStore(str(tmp_path / "state.json")),
            sync_timeout_ms=0,
        )
        try:
            await auditor.poll_once()
        finally:
            await core_client.aclose()

        bodies = [
            r.get("content", {}).get("body")
            for r in auditor.sink.records  # type: ignore[attr-defined]
        ]
        assert "audit me" in bodies
