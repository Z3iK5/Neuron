# SPDX-License-Identifier: Apache-2.0
"""Tests for the console's Federation health page, the ``federation_destinations``
store (migration 29) and the per-destination "Retry now" action."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest_asyncio
from fastapi.testclient import TestClient

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.storage import destinations as destinations_store
from neuron_server.storage import outbox as outbox_store
from neuron_server.storage.database import Database, connect_database
from neuron_server.storage.migrations import run_migrations

_LOGIN = "/console/login"


# --- store unit tests -------------------------------------------------------
@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = connect_database("sqlite:///:memory:")
    await database.connect()
    await run_migrations(database)
    try:
        yield database
    finally:
        await database.disconnect()


async def test_failure_increments_then_success_resets(db: Database) -> None:
    await destinations_store.record_failure(db, "b.test", 1000, "ConnectError: refused")
    await destinations_store.record_failure(db, "b.test", 2000, "ConnectError: refused")
    rows = {r.destination: r for r in await destinations_store.list_destinations(db)}
    assert rows["b.test"].consecutive_failures == 2
    assert rows["b.test"].last_failure_ts == 2000
    assert rows["b.test"].last_error == "ConnectError: refused"

    # A success resets the run to 0 and clears the error, keeping success ts.
    await destinations_store.record_success(db, "b.test", 3000)
    rows = {r.destination: r for r in await destinations_store.list_destinations(db)}
    assert rows["b.test"].consecutive_failures == 0
    assert rows["b.test"].last_success_ts == 3000
    assert rows["b.test"].last_error is None

    # A failure after the reset starts the run again at 1.
    await destinations_store.record_failure(db, "b.test", 4000, "Timeout")
    rows = {r.destination: r for r in await destinations_store.list_destinations(db)}
    assert rows["b.test"].consecutive_failures == 1


async def test_pending_backlog_combines_pdu_and_edu(db: Database) -> None:
    first = await outbox_store.enqueue(db, "b.test", {"n": 1})
    await outbox_store.enqueue(db, "b.test", {"n": 2})
    await outbox_store.enqueue_edu(db, "b.test", {"edu_type": "m.receipt"})
    await outbox_store.enqueue(db, "c.test", {"n": 1})

    backlog = await destinations_store.pending_backlog(db)
    assert backlog["b.test"].pdu_pending == 2
    assert backlog["b.test"].edu_pending == 1
    assert backlog["b.test"].oldest_stream_id == first
    assert backlog["c.test"].pdu_pending == 1
    assert backlog["c.test"].edu_pending == 0


# --- console page -----------------------------------------------------------
def _client(tmp_path: Path) -> TestClient:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        first_user_admin=True,
        public_base_url="http://localhost:8008",
    )
    return TestClient(create_app(settings))


def _csrf(text: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert m, "no CSRF token found in page"
    return m.group(1)


def _signup(client: TestClient, username: str, password: str) -> None:
    resp = client.post("/get-started", data={"username": username, "password": password})
    assert resp.status_code == 200, resp.text


def _login(client: TestClient, username: str, password: str) -> None:
    token = _csrf(client.get(_LOGIN).text)
    resp = client.post(
        _LOGIN,
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def _seed_federation(tmp_path: Path) -> None:
    """Seed a failing destination + a pending outbox row via a separate sqlite
    connection (the app reads them back through its async connection)."""
    conn = sqlite3.connect(tmp_path / "hs.db", timeout=5)
    try:
        conn.execute(
            "INSERT INTO federation_destinations"
            " (destination, last_success_ts, last_failure_ts, consecutive_failures, last_error)"
            " VALUES (?, ?, ?, ?, ?)",
            ("down.test", 1000, 5000, 4, "ConnectError: connection refused"),
        )
        conn.execute(
            "INSERT INTO federation_outbox (stream_id, destination, pdu_json, leased_until)"
            " VALUES (?, ?, ?, 0)",
            (1, "down.test", "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_federation_requires_console_admin(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/console/federation", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == _LOGIN


def test_federation_page_lists_a_failing_destination(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _login(client, "admin", "s3cret-password")
        _seed_federation(tmp_path)

        page = client.get("/console/federation")
        assert page.status_code == 200
        assert "down.test" in page.text
        assert "failing" in page.text  # 4 consecutive failures -> failing
        assert "connection refused" in page.text
        assert ">1<" in page.text  # one pending PDU


def test_federation_empty_state(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _login(client, "admin", "s3cret-password")
        page = client.get("/console/federation")
        assert page.status_code == 200
        assert "No federation activity yet" in page.text


def test_retry_action_needs_csrf_and_calls_sender(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _signup(client, "admin", "s3cret-password")
        _login(client, "admin", "s3cret-password")
        _seed_federation(tmp_path)  # a row so the page renders a Retry form (with CSRF)
        spy = AsyncMock()
        client.app.state.federation_sender.retry = spy  # type: ignore[attr-defined]

        # Missing CSRF is rejected and the sender is never called.
        bad = client.post(
            "/console/federation/down.test/retry", data={}, follow_redirects=False
        )
        assert bad.status_code == 400
        spy.assert_not_called()

        # A valid CSRF token drives the sender's retry for that destination.
        token = _csrf(client.get("/console/federation").text)
        good = client.post(
            "/console/federation/down.test/retry",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert good.status_code == 303
        assert good.headers["location"] == "/console/federation"
        spy.assert_awaited_once_with("down.test")
