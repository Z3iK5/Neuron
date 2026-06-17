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
