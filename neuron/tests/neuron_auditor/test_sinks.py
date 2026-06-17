"""Tests for the audit record schema and the sinks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from neuron_auditor.sinks import FileSink, S3Sink, build_audit_record


def test_build_audit_record_basic() -> None:
    event = {
        "event_id": "$1",
        "type": "m.room.message",
        "sender": "@alice:hs",
        "origin_server_ts": 1234,
        "content": {"body": "hello", "msgtype": "m.text"},
    }
    record = build_audit_record("!room:hs", event)
    assert record["room_id"] == "!room:hs"
    assert record["event_id"] == "$1"
    assert record["type"] == "m.room.message"
    assert record["content"] == {"body": "hello", "msgtype": "m.text"}
    assert record["encrypted"] is False
    assert record["decrypted"] is True
    assert "state_key" not in record
    assert "audited_at" in record


def test_build_audit_record_state_and_encrypted() -> None:
    state_ev = {"event_id": "$2", "type": "m.room.member", "state_key": "@bob:hs", "content": {}}
    assert build_audit_record("!r:hs", state_ev)["state_key"] == "@bob:hs"

    enc = {"event_id": "$3", "type": "m.room.encrypted", "content": {"ciphertext": "x"}}
    rec = build_audit_record("!r:hs", enc)
    assert rec["encrypted"] is True
    assert rec["decrypted"] is False  # Phase 4 cannot decrypt; recorded, not dropped


def test_file_sink_appends_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    sink = FileSink(str(path))
    sink.write({"event_id": "$1", "room_id": "!r:hs"})
    sink.write({"event_id": "$2", "room_id": "!r:hs"})

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event_id"] == "$1"
    assert json.loads(lines[1])["event_id"] == "$2"


class _FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)


def test_s3_sink_puts_one_object_per_event() -> None:
    fake = _FakeS3()
    sink = S3Sink(bucket="audit-bucket", prefix="audit", client=fake)
    sink.write({"event_id": "$abc", "room_id": "!room:hs", "type": "m.room.message"})

    assert len(fake.puts) == 1
    put = fake.puts[0]
    assert put["Bucket"] == "audit-bucket"
    assert put["Key"] == "audit/!room:hs/$abc.json"
    assert json.loads(put["Body"].decode())["event_id"] == "$abc"
    assert put["ContentType"] == "application/json"
