"""Tests for the Auditor sync/record loop (uses fakes; no live server)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from neuron_auditor.core import Auditor
from neuron_auditor.state import StateStore


class FakeClient:
    """Returns scripted /sync responses and records join/since calls."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.joined: list[str] = []
        self.since_seen: list[str | None] = []

    async def sync(self, *, since: str | None = None, timeout_ms: int = 30000) -> dict[str, Any]:
        self.since_seen.append(since)
        return self._responses.pop(0) if self._responses else {"next_batch": "end", "rooms": {}}

    async def join_room(self, room_id_or_alias: str) -> dict[str, Any]:
        self.joined.append(room_id_or_alias)
        return {"room_id": room_id_or_alias}


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


def _sync_with_message(next_batch: str) -> dict[str, Any]:
    return {
        "next_batch": next_batch,
        "rooms": {
            "join": {
                "!r:hs": {
                    "timeline": {
                        "events": [
                            {
                                "event_id": "$1",
                                "type": "m.room.message",
                                "sender": "@a:hs",
                                "content": {"body": "hi"},
                            }
                        ]
                    }
                }
            }
        },
    }


async def test_poll_records_events_and_saves_token(tmp_path: Path) -> None:
    client = FakeClient([_sync_with_message("s1")])
    sink = RecordingSink()
    state = StateStore(str(tmp_path / "state.json"))
    auditor = Auditor(client, sink, state)  # type: ignore[arg-type]

    written = await auditor.poll_once()

    assert written == 1
    assert sink.records[0]["event_id"] == "$1"
    assert sink.records[0]["room_id"] == "!r:hs"
    assert state.get_since() == "s1"  # token persisted for restart safety


async def test_restart_resumes_from_saved_token(tmp_path: Path) -> None:
    state_path = str(tmp_path / "state.json")
    StateStore(state_path).set_since("s1")  # simulate a prior run

    client = FakeClient([{"next_batch": "s2", "rooms": {}}])
    auditor = Auditor(client, RecordingSink(), StateStore(state_path))  # type: ignore[arg-type]
    await auditor.poll_once()

    # The first (and only) sync must have been made with the saved token.
    assert client.since_seen == ["s1"]


async def test_auto_join_invited_rooms(tmp_path: Path) -> None:
    response = {"next_batch": "s1", "rooms": {"invite": {"!invited:hs": {}}}}
    client = FakeClient([response])
    auditor = Auditor(client, RecordingSink(), StateStore(str(tmp_path / "s.json")))  # type: ignore[arg-type]

    await auditor.poll_once()
    assert client.joined == ["!invited:hs"]


async def test_encrypted_event_is_recorded_not_dropped(tmp_path: Path) -> None:
    response = {
        "next_batch": "s1",
        "rooms": {
            "join": {
                "!r:hs": {
                    "timeline": {
                        "events": [
                            {"event_id": "$e", "type": "m.room.encrypted",
                             "sender": "@a:hs", "content": {"ciphertext": "x"}}
                        ]
                    }
                }
            }
        },
    }
    sink = RecordingSink()
    auditor = Auditor(FakeClient([response]), sink, StateStore(str(tmp_path / "s.json")))  # type: ignore[arg-type]
    await auditor.poll_once()
    assert sink.records[0]["encrypted"] is True
    assert sink.records[0]["decrypted"] is False
