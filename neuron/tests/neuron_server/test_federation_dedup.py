# SPDX-License-Identifier: Apache-2.0
"""Inbound federation transaction dedup: a replayed transaction is a no-op."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest_asyncio

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.storage import transactions as txn_store
from neuron_server.storage.database import connect_database
from neuron_server.storage.migrations import run_migrations


@pytest_asyncio.fixture
async def db() -> AsyncIterator[object]:
    database = connect_database("sqlite:///:memory:")
    await database.connect()
    await run_migrations(database)
    try:
        yield database
    finally:
        await database.disconnect()


async def test_mark_received_is_idempotent(db: object) -> None:
    assert await txn_store.was_received(db, "a.test", "t1") is False
    await txn_store.mark_received(db, "a.test", "t1", 1)
    assert await txn_store.was_received(db, "a.test", "t1") is True
    await txn_store.mark_received(db, "a.test", "t1", 2)  # no error on a repeat
    # A different (origin, txn_id) is tracked separately.
    assert await txn_store.was_received(db, "a.test", "t2") is False
    assert await txn_store.was_received(db, "b.test", "t1") is False


def _opener(target_app: object):  # noqa: ANN202 - test helper
    def open_client(server_name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target_app), base_url=f"https://{server_name}"
        )

    return open_client


async def test_replayed_transaction_is_not_reprocessed(tmp_path: Path) -> None:
    app_a = create_app(
        NeuronServerSettings(name="a.test", database_url=f"sqlite:///{tmp_path / 'a.db'}")
    )
    app_b = create_app(
        NeuronServerSettings(name="b.test", database_url=f"sqlite:///{tmp_path / 'b.db'}")
    )
    async with app_b.router.lifespan_context(app_b), app_a.router.lifespan_context(app_a):
        app_a.state.federation_client.open_client = _opener(app_b)
        app_b.state.federation_client.open_client = _opener(app_a)

        # An (invalid) PDU, so the first processing yields a per-PDU result; the
        # dedup short-circuit happens before validation, so the replay yields none.
        txn = {
            "origin": "a.test",
            "origin_server_ts": 1,
            "pdus": [
                {
                    "type": "m.room.message",
                    "sender": "@u:a.test",
                    "room_id": "!r:b.test",
                    "content": {},
                    "origin_server_ts": 0,
                    "depth": 1,
                    "prev_events": [],
                    "auth_events": [],
                }
            ],
            "edus": [],
        }
        path = "/_matrix/federation/v1/send/fixed-txn-1"

        first = await app_a.state.federation_client.put_json("b.test", path, txn)
        second = await app_a.state.federation_client.put_json("b.test", path, txn)

        assert first["pdus"]  # processed (a per-PDU result is reported)
        assert second["pdus"] == {}  # replay short-circuited, nothing reprocessed
