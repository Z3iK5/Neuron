# SPDX-License-Identifier: Apache-2.0
"""Schema migrations for ``neuron_server``.

Migrations are an ordered list of :class:`Migration` records, each a set of SQL
statements. :func:`run_migrations` applies any that haven't run yet (tracked in a
``schema_migrations`` table) and is **idempotent** — safe to run on every start.

SQL is written portably (``?`` placeholders, ``IF NOT EXISTS``, ``ON CONFLICT``)
so the same statements work on both SQLite and PostgreSQL. Later phases append new
migrations; we never edit a migration that has shipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from neuron_server.storage.database import Database


@dataclass(frozen=True)
class Migration:
    """One ordered schema change: a version, a name, and its SQL statements."""

    version: int
    name: str
    statements: tuple[str, ...]


# The ordered migration history. HS-0 only needs a place to record server-level
# metadata (e.g. the server name and, later, its signing key). Domain tables
# (users, devices, rooms, events, ...) are added by later phases as new entries.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_metadata",
        statements=(
            "CREATE TABLE IF NOT EXISTS server_metadata ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL"
            ")",
        ),
    ),
    Migration(
        version=2,
        name="auth_accounts",
        statements=(
            # Local user accounts. ``name`` is the full @localpart:server_name.
            "CREATE TABLE IF NOT EXISTS users ("
            " name TEXT PRIMARY KEY,"
            " password_hash TEXT,"
            " admin INTEGER NOT NULL DEFAULT 0,"
            " deactivated INTEGER NOT NULL DEFAULT 0,"
            " created_ts INTEGER NOT NULL"
            ")",
            # A user's logged-in devices.
            "CREATE TABLE IF NOT EXISTS devices ("
            " user_id TEXT NOT NULL,"
            " device_id TEXT NOT NULL,"
            " display_name TEXT,"
            " created_ts INTEGER NOT NULL,"
            " PRIMARY KEY (user_id, device_id)"
            ")",
            # Bearer access tokens, each bound to a (user, device).
            "CREATE TABLE IF NOT EXISTS access_tokens ("
            " token TEXT PRIMARY KEY,"
            " user_id TEXT NOT NULL,"
            " device_id TEXT NOT NULL,"
            " created_ts INTEGER NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_devices_user ON devices (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_tokens_user ON access_tokens (user_id)",
        ),
    ),
    Migration(
        version=3,
        name="rooms_events_state",
        statements=(
            "CREATE TABLE IF NOT EXISTS rooms ("
            " room_id TEXT PRIMARY KEY,"
            " creator TEXT NOT NULL,"
            " room_version TEXT NOT NULL,"
            " created_ts INTEGER NOT NULL"
            ")",
            "CREATE TABLE IF NOT EXISTS events ("
            " event_id TEXT PRIMARY KEY,"
            " room_id TEXT NOT NULL,"
            " type TEXT NOT NULL,"
            " state_key TEXT,"
            " sender TEXT NOT NULL,"
            " content TEXT NOT NULL,"
            " origin_server_ts INTEGER NOT NULL,"
            " depth INTEGER NOT NULL,"
            " stream_ordering INTEGER NOT NULL,"
            " unsigned TEXT,"
            " redacts TEXT"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_events_room_stream"
            " ON events (room_id, stream_ordering)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_stream ON events (stream_ordering)",
            "CREATE TABLE IF NOT EXISTS current_state ("
            " room_id TEXT NOT NULL,"
            " type TEXT NOT NULL,"
            " state_key TEXT NOT NULL,"
            " event_id TEXT NOT NULL,"
            " PRIMARY KEY (room_id, type, state_key)"
            ")",
            "CREATE TABLE IF NOT EXISTS room_memberships ("
            " room_id TEXT NOT NULL,"
            " user_id TEXT NOT NULL,"
            " membership TEXT NOT NULL,"
            " PRIMARY KEY (room_id, user_id)"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_memberships_user ON room_memberships (user_id)",
            "CREATE TABLE IF NOT EXISTS event_txns ("
            " user_id TEXT NOT NULL,"
            " txn_id TEXT NOT NULL,"
            " event_id TEXT NOT NULL,"
            " PRIMARY KEY (user_id, txn_id)"
            ")",
        ),
    ),
    Migration(
        version=4,
        name="media_repository",
        statements=(
            "CREATE TABLE IF NOT EXISTS media ("
            " media_id TEXT PRIMARY KEY,"
            " content_type TEXT NOT NULL,"
            " upload_name TEXT,"
            " size INTEGER NOT NULL,"
            " uploader TEXT NOT NULL,"
            " created_ts INTEGER NOT NULL"
            ")",
        ),
    ),
    Migration(
        version=5,
        name="e2ee_relay",
        statements=(
            "CREATE TABLE IF NOT EXISTS device_keys ("
            " user_id TEXT NOT NULL,"
            " device_id TEXT NOT NULL,"
            " key_json TEXT NOT NULL,"
            " PRIMARY KEY (user_id, device_id)"
            ")",
            "CREATE TABLE IF NOT EXISTS one_time_keys ("
            " user_id TEXT NOT NULL,"
            " device_id TEXT NOT NULL,"
            " key_alg_id TEXT NOT NULL,"
            " key_json TEXT NOT NULL,"
            " PRIMARY KEY (user_id, device_id, key_alg_id)"
            ")",
            "CREATE TABLE IF NOT EXISTS fallback_keys ("
            " user_id TEXT NOT NULL,"
            " device_id TEXT NOT NULL,"
            " algorithm TEXT NOT NULL,"
            " key_alg_id TEXT NOT NULL,"
            " key_json TEXT NOT NULL,"
            " used INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (user_id, device_id, algorithm)"
            ")",
            "CREATE TABLE IF NOT EXISTS cross_signing_keys ("
            " user_id TEXT NOT NULL,"
            " key_type TEXT NOT NULL,"
            " key_json TEXT NOT NULL,"
            " PRIMARY KEY (user_id, key_type)"
            ")",
            "CREATE TABLE IF NOT EXISTS to_device_messages ("
            " stream_id INTEGER PRIMARY KEY,"
            " target_user TEXT NOT NULL,"
            " target_device TEXT NOT NULL,"
            " sender TEXT NOT NULL,"
            " type TEXT NOT NULL,"
            " content_json TEXT NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_to_device_target"
            " ON to_device_messages (target_user, target_device, stream_id)",
            "CREATE TABLE IF NOT EXISTS device_list_changes ("
            " stream_id INTEGER PRIMARY KEY,"
            " user_id TEXT NOT NULL"
            ")",
        ),
    ),
    Migration(
        version=6,
        name="profiles_accountdata_filters_regtokens",
        statements=(
            "CREATE TABLE IF NOT EXISTS profiles ("
            " user_id TEXT PRIMARY KEY,"
            " displayname TEXT,"
            " avatar_url TEXT"
            ")",
            "CREATE TABLE IF NOT EXISTS account_data ("
            " user_id TEXT NOT NULL,"
            " room_id TEXT NOT NULL,"
            " type TEXT NOT NULL,"
            " content_json TEXT NOT NULL,"
            " PRIMARY KEY (user_id, room_id, type)"
            ")",
            "CREATE TABLE IF NOT EXISTS filters ("
            " user_id TEXT NOT NULL,"
            " filter_id TEXT NOT NULL,"
            " definition_json TEXT NOT NULL,"
            " PRIMARY KEY (user_id, filter_id)"
            ")",
            "CREATE TABLE IF NOT EXISTS registration_tokens ("
            " token TEXT PRIMARY KEY,"
            " uses_allowed INTEGER,"
            " pending INTEGER NOT NULL DEFAULT 0,"
            " completed INTEGER NOT NULL DEFAULT 0,"
            " expiry_time INTEGER"
            ")",
        ),
    ),
    Migration(
        version=7,
        name="event_pdu_json",
        # The full signed federation event (auth_events/prev_events/hashes/
        # signatures), so events can be served and verified over federation.
        statements=("ALTER TABLE events ADD COLUMN pdu_json TEXT",),
    ),
)


async def run_migrations(db: Database, migrations: tuple[Migration, ...] = MIGRATIONS) -> list[int]:
    """Apply any not-yet-applied migrations in order; return the versions applied.

    Each migration runs in its own transaction together with the bookkeeping row,
    so a partially-applied migration never leaves the schema half-updated.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL"
        ")"
    )
    rows = await db.fetchall("SELECT version FROM schema_migrations")
    applied = {int(row[0]) for row in rows}

    newly_applied: list[int] = []
    for migration in sorted(migrations, key=lambda m: m.version):
        if migration.version in applied:
            continue
        async with db.transaction():
            for statement in migration.statements:
                await db.execute(statement)
            await db.execute(
                "INSERT INTO schema_migrations (version, name, applied_at)"
                " VALUES (?, ?, ?)",
                (migration.version, migration.name, datetime.now(UTC).isoformat()),
            )
        newly_applied.append(migration.version)
    return newly_applied
