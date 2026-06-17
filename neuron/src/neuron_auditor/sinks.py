"""Output sinks for audited events, plus the stable JSON record schema.

A *sink* is anything with a ``write(record)`` method. We ship two:

- ``FileSink`` — appends one JSON object per line (JSON Lines / ``.jsonl``).
- ``S3Sink``  — stores one JSON object per event in an S3-compatible bucket.

``build_audit_record`` turns a raw Matrix event into the stable envelope we
record, so the on-disk format doesn't change if Matrix event shapes evolve.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from neuron_crypto.base import DecryptResult


def build_audit_record(
    room_id: str, event: dict[str, Any], decrypt: DecryptResult | None = None
) -> dict[str, Any]:
    """Build the stable audit envelope for one Matrix event.

    For an ``m.room.encrypted`` event, pass the ``decrypt`` result:
    - if it decrypted, the record carries the *inner* (cleartext) type/content and
      ``"decrypted": true``;
    - otherwise the record keeps the encrypted envelope, ``"decrypted": false``,
      and a ``"decryption_error"`` reason — encrypted events are recorded, never
      silently dropped.
    """
    event_type = event.get("type", "")
    is_encrypted = event_type == "m.room.encrypted"
    record: dict[str, Any] = {
        "audited_at": datetime.now(tz=UTC).isoformat(),
        "room_id": room_id,
        "event_id": event.get("event_id"),
        "sender": event.get("sender"),
        "origin_server_ts": event.get("origin_server_ts"),
        "encrypted": is_encrypted,
    }

    if is_encrypted and decrypt is not None and decrypt.decrypted:
        record["type"] = decrypt.event_type or event_type
        record["content"] = decrypt.content
        record["decrypted"] = True
    elif is_encrypted:
        record["type"] = event_type
        record["content"] = event.get("content")
        record["decrypted"] = False
        if decrypt is not None and decrypt.reason:
            record["decryption_error"] = decrypt.reason
    else:
        record["type"] = event_type
        record["content"] = event.get("content")
        record["decrypted"] = True

    if "state_key" in event:
        record["state_key"] = event["state_key"]
    return record


class Sink(Protocol):
    """Anything that can persist an audit record."""

    def write(self, record: dict[str, Any]) -> None: ...


class FileSink:
    """Append audit records to a local JSON Lines file."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


class S3Sink:
    """Store one JSON object per event in an S3-compatible bucket.

    ``client`` is a boto3-style S3 client; tests inject a fake. Use
    :meth:`from_settings` to build a real one from configuration.
    """

    def __init__(self, *, bucket: str, prefix: str, client: Any) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = client

    def write(self, record: dict[str, Any]) -> None:
        room = record.get("room_id", "unknown")
        event_id = record.get("event_id", "unknown")
        key = f"{self._prefix}/{room}/{event_id}.json"
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(record).encode("utf-8"),
            ContentType="application/json",
        )

    @classmethod
    def from_settings(cls, settings: Any) -> S3Sink:
        # Imported lazily so the file-only path doesn't require boto3.
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=settings.auditor_s3_endpoint_url or None,
            aws_access_key_id=settings.auditor_s3_access_key.get_secret_value() or None,
            aws_secret_access_key=settings.auditor_s3_secret_key.get_secret_value() or None,
            region_name=settings.auditor_s3_region,
        )
        return cls(
            bucket=settings.auditor_s3_bucket,
            prefix=settings.auditor_s3_prefix,
            client=client,
        )


class CompositeSink:
    """Fan a record out to several sinks (e.g. file AND S3)."""

    def __init__(self, sinks: list[Sink]) -> None:
        self._sinks = sinks

    def write(self, record: dict[str, Any]) -> None:
        for sink in self._sinks:
            sink.write(record)


def make_sink(settings: Any) -> Sink:
    """Build the configured sink(s) from settings (``file`` / ``s3`` / ``both``)."""
    choice = settings.auditor_sink.lower()
    sinks: list[Sink] = []
    if choice in ("file", "both"):
        sinks.append(FileSink(settings.auditor_file_path))
    if choice in ("s3", "both"):
        sinks.append(S3Sink.from_settings(settings))
    if not sinks:
        raise ValueError(f"Unknown auditor_sink: {settings.auditor_sink!r} (use file|s3|both).")
    return sinks[0] if len(sinks) == 1 else CompositeSink(sinks)
