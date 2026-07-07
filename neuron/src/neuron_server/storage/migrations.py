# SPDX-License-Identifier: Apache-2.0
"""Schema migrations for ``neuron_server``.

Migrations are an ordered list of :class:`Migration` records, each a set of SQL
statements. :func:`run_migrations` applies any that haven't run yet (tracked in a
``schema_migrations`` table) and is **idempotent** — safe to run on every start.

SQL is written portably (``?`` placeholders, ``IF NOT EXISTS``, ``ON CONFLICT``)
so the same statements work on both SQLite and PostgreSQL. Integer columns are
declared ``BIGINT`` because PostgreSQL's ``INTEGER`` is only 32 bits — too small
for millisecond timestamps and stream positions — whereas SQLite treats ``BIGINT``
as the same flexible-width INTEGER affinity, so existing SQLite databases are
unaffected. Later phases append new migrations; we never edit a *shipped* migration
in a way that changes an already-created table (these type names only take effect
on a fresh ``CREATE TABLE``).
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
            " admin BIGINT NOT NULL DEFAULT 0,"
            " deactivated BIGINT NOT NULL DEFAULT 0,"
            " created_ts BIGINT NOT NULL"
            ")",
            # A user's logged-in devices.
            "CREATE TABLE IF NOT EXISTS devices ("
            " user_id TEXT NOT NULL,"
            " device_id TEXT NOT NULL,"
            " display_name TEXT,"
            " created_ts BIGINT NOT NULL,"
            " PRIMARY KEY (user_id, device_id)"
            ")",
            # Bearer access tokens, each bound to a (user, device).
            "CREATE TABLE IF NOT EXISTS access_tokens ("
            " token TEXT PRIMARY KEY,"
            " user_id TEXT NOT NULL,"
            " device_id TEXT NOT NULL,"
            " created_ts BIGINT NOT NULL"
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
            " created_ts BIGINT NOT NULL"
            ")",
            "CREATE TABLE IF NOT EXISTS events ("
            " event_id TEXT PRIMARY KEY,"
            " room_id TEXT NOT NULL,"
            " type TEXT NOT NULL,"
            " state_key TEXT,"
            " sender TEXT NOT NULL,"
            " content TEXT NOT NULL,"
            " origin_server_ts BIGINT NOT NULL,"
            " depth BIGINT NOT NULL,"
            " stream_ordering BIGINT NOT NULL,"
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
            " size BIGINT NOT NULL,"
            " uploader TEXT NOT NULL,"
            " created_ts BIGINT NOT NULL"
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
            " used BIGINT NOT NULL DEFAULT 0,"
            " PRIMARY KEY (user_id, device_id, algorithm)"
            ")",
            "CREATE TABLE IF NOT EXISTS cross_signing_keys ("
            " user_id TEXT NOT NULL,"
            " key_type TEXT NOT NULL,"
            " key_json TEXT NOT NULL,"
            " PRIMARY KEY (user_id, key_type)"
            ")",
            "CREATE TABLE IF NOT EXISTS to_device_messages ("
            " stream_id BIGINT PRIMARY KEY,"
            " target_user TEXT NOT NULL,"
            " target_device TEXT NOT NULL,"
            " sender TEXT NOT NULL,"
            " type TEXT NOT NULL,"
            " content_json TEXT NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_to_device_target"
            " ON to_device_messages (target_user, target_device, stream_id)",
            "CREATE TABLE IF NOT EXISTS device_list_changes ("
            " stream_id BIGINT PRIMARY KEY,"
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
            " uses_allowed BIGINT,"
            " pending BIGINT NOT NULL DEFAULT 0,"
            " completed BIGINT NOT NULL DEFAULT 0,"
            " expiry_time BIGINT"
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
    Migration(
        version=8,
        name="remote_server_keys",
        # Cache of other servers' Ed25519 verify keys (fetched from their
        # /_matrix/key/v2/server), used to authenticate inbound federation.
        statements=(
            "CREATE TABLE IF NOT EXISTS remote_server_keys ("
            " server_name TEXT NOT NULL,"
            " key_id TEXT NOT NULL,"
            " verify_key TEXT NOT NULL,"
            " valid_until_ts BIGINT NOT NULL,"
            " PRIMARY KEY (server_name, key_id)"
            ")",
        ),
    ),
    Migration(
        version=9,
        name="federated_invites",
        # Invites received over federation for a *local* user to a room hosted
        # elsewhere (we don't host the room, so this is tracked separately).
        statements=(
            "CREATE TABLE IF NOT EXISTS federated_invites ("
            " user_id TEXT NOT NULL,"
            " room_id TEXT NOT NULL,"
            " event_json TEXT NOT NULL,"
            " invite_state_json TEXT NOT NULL,"
            " PRIMARY KEY (user_id, room_id)"
            ")",
        ),
    ),
    Migration(
        version=10,
        name="federated_invite_stream",
        # A stream position so /sync can tell which invites are new.
        statements=(
            "ALTER TABLE federated_invites ADD COLUMN stream_id BIGINT NOT NULL DEFAULT 0",
        ),
    ),
    Migration(
        version=11,
        name="receipts",
        # Read receipts (local and received over federation), with a stream
        # position so /sync can report only changed ones.
        statements=(
            "CREATE TABLE IF NOT EXISTS receipts ("
            " room_id TEXT NOT NULL,"
            " user_id TEXT NOT NULL,"
            " receipt_type TEXT NOT NULL,"
            " event_id TEXT NOT NULL,"
            " ts BIGINT NOT NULL,"
            " stream_id BIGINT NOT NULL,"
            " PRIMARY KEY (room_id, user_id, receipt_type)"
            ")",
        ),
    ),
    Migration(
        version=12,
        name="federation_outbox",
        # Events that failed to send to a destination server, queued for retry.
        statements=(
            "CREATE TABLE IF NOT EXISTS federation_outbox ("
            " stream_id BIGINT PRIMARY KEY,"
            " destination TEXT NOT NULL,"
            " pdu_json TEXT NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_outbox_destination"
            " ON federation_outbox (destination, stream_id)",
        ),
    ),
    Migration(
        version=13,
        name="moderation",
        # Real backing for the admin moderation actions: a shadow-ban flag, a room
        # block list, spec-shaped delete/redact status rows, abuse reports, and the
        # per-user server-notices room mapping. (ADD COLUMN is portable across the
        # SQLite/PostgreSQL backends, as in earlier migrations.)
        statements=(
            "ALTER TABLE users ADD COLUMN shadow_banned BIGINT NOT NULL DEFAULT 0",
            # Rooms an operator has blocked on this server (joins/sends are refused).
            "CREATE TABLE IF NOT EXISTS blocked_rooms ("
            " room_id TEXT PRIMARY KEY,"
            " blocked_by TEXT,"
            " blocked_ts BIGINT NOT NULL"
            ")",
            # Result of an admin room deletion/purge (done synchronously here).
            "CREATE TABLE IF NOT EXISTS room_deletions ("
            " delete_id TEXT PRIMARY KEY,"
            " room_id TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " kicked_users TEXT NOT NULL,"
            " created_ts BIGINT NOT NULL"
            ")",
            # Result of an admin bulk redaction of a user's events.
            "CREATE TABLE IF NOT EXISTS room_redactions ("
            " redact_id TEXT PRIMARY KEY,"
            " user_id TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " total BIGINT NOT NULL,"
            " failed TEXT NOT NULL,"
            " created_ts BIGINT NOT NULL"
            ")",
            # Abuse reports about events, submitted by users; listed in the console.
            "CREATE TABLE IF NOT EXISTS event_reports ("
            " id TEXT PRIMARY KEY,"
            " room_id TEXT NOT NULL,"
            " event_id TEXT NOT NULL,"
            " reporter TEXT NOT NULL,"
            " reason TEXT,"
            " score BIGINT,"
            " received_ts BIGINT NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_event_reports_ts"
            " ON event_reports (received_ts)",
            # Maps a target user to their single (reused) server-notices room.
            "CREATE TABLE IF NOT EXISTS server_notices_rooms ("
            " user_id TEXT PRIMARY KEY,"
            " room_id TEXT NOT NULL"
            ")",
        ),
    ),
    Migration(
        version=14,
        name="passkeys",
        # WebAuthn passkeys for console sign-in, owned by a (admin) user account.
        statements=(
            "CREATE TABLE IF NOT EXISTS passkeys ("
            " credential_id TEXT PRIMARY KEY,"
            " owner TEXT NOT NULL,"
            " public_key TEXT NOT NULL,"
            " sign_count BIGINT NOT NULL,"
            " label TEXT NOT NULL,"
            " created_ts BIGINT NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_passkeys_owner ON passkeys (owner)",
        ),
    ),
    Migration(
        version=15,
        name="typing",
        # Cross-process typing state. One row per (room, user) ever seen; rows are
        # UPSERTed (expiry_ms in the past = not typing) and never deleted, so
        # MAX(stream_id) is a monotonic serial that /sync can compare across
        # workers. A worker shows a user as typing while expiry_ms > now.
        statements=(
            "CREATE TABLE IF NOT EXISTS typing ("
            " room_id TEXT NOT NULL,"
            " user_id TEXT NOT NULL,"
            " expiry_ms BIGINT NOT NULL,"
            " stream_id BIGINT NOT NULL,"
            " PRIMARY KEY (room_id, user_id)"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_typing_room ON typing (room_id, expiry_ms)",
        ),
    ),
    Migration(
        version=16,
        name="stream_positions",
        # Per-writer contiguous "persisted upto" position for each id stream. A
        # reader's safe floor is MIN(stream_id) across instances: a writer holding
        # a low in-flight id keeps its row (and so the floor) back until it commits,
        # so /sync never advances past an id that is allocated but not yet committed
        # (the multi-writer lost-event gap). Postgres maintains it; SQLite ignores
        # this table and reads MAX(col) directly (single connection => no gap).
        statements=(
            "CREATE TABLE IF NOT EXISTS stream_positions ("
            " stream_name TEXT NOT NULL,"
            " instance_name TEXT NOT NULL,"
            " stream_id BIGINT NOT NULL,"
            " PRIMARY KEY (stream_name, instance_name)"
            ")",
        ),
    ),
    Migration(
        version=17,
        name="received_transactions",
        # Dedup inbound federation transactions: a remote server retries a txn it
        # didn't get an ack for, possibly to a different worker. Recording (origin,
        # txn_id) lets a replay short-circuit instead of re-validating/re-applying.
        statements=(
            "CREATE TABLE IF NOT EXISTS received_transactions ("
            " origin TEXT NOT NULL,"
            " txn_id TEXT NOT NULL,"
            " received_ts BIGINT NOT NULL,"
            " PRIMARY KEY (origin, txn_id)"
            ")",
        ),
    ),
    Migration(
        version=18,
        name="federation_outbox_lease",
        # Single-owner outbox draining: with >1 worker each flusher would otherwise
        # drain the same queue and double-send. A worker leases a destination's rows
        # (owner + leased_until) before sending; others skip leased rows, and a
        # crashed worker's lease expires so the backlog is retried.
        statements=(
            "ALTER TABLE federation_outbox ADD COLUMN leased_until BIGINT NOT NULL DEFAULT 0",
            "ALTER TABLE federation_outbox ADD COLUMN owner TEXT",
        ),
    ),
    Migration(
        version=19,
        name="uia_sessions",
        # Shared User-Interactive-Auth sessions: registration is a 401-challenge then
        # retry. With >1 worker (and no sticky load balancer) the retry can land on a
        # different worker, so the open session must live in the database, not in one
        # worker's memory. A background sweep removes expired rows (created_ts).
        statements=(
            "CREATE TABLE IF NOT EXISTS uia_sessions ("
            " session_id TEXT PRIMARY KEY,"
            " created_ts BIGINT NOT NULL"
            ")",
        ),
    ),
    Migration(
        version=20,
        name="account_data_stream",
        # A stream position on account data so /sync can deliver only rows changed
        # since a client's token (same pattern as receipts). Existing rows keep
        # stream_id 0: an initial sync returns everything regardless, and any later
        # write re-stamps the row with a fresh id.
        statements=(
            "ALTER TABLE account_data ADD COLUMN stream_id BIGINT NOT NULL DEFAULT 0",
        ),
    ),
    Migration(
        version=21,
        name="push_rules",
        # Per-user push rules (global scope only — the only scope the spec defines).
        # One table holds both: custom rules (full definition) and per-rule tweaks
        # to the server-default `.m.*` rules (only `enabled`/`actions_json` set,
        # `conditions_json`/`pattern` NULL). The computed spec-default ruleset is
        # merged with these rows on read; NULL means "no override".
        statements=(
            "CREATE TABLE IF NOT EXISTS push_rules ("
            " user_id TEXT NOT NULL,"
            " kind TEXT NOT NULL,"
            " rule_id TEXT NOT NULL,"
            " ordering BIGINT NOT NULL DEFAULT 0,"
            " conditions_json TEXT,"
            " actions_json TEXT,"
            " pattern TEXT,"
            " enabled BIGINT,"
            " PRIMARY KEY (user_id, kind, rule_id)"
            ")",
        ),
    ),
    Migration(
        version=22,
        name="membership_forgotten",
        # POST /rooms/{id}/forget: a forgotten membership row is hidden from /sync
        # and room listings but kept (re-joining resets the flag via the upsert).
        statements=(
            "ALTER TABLE room_memberships ADD COLUMN forgotten BIGINT NOT NULL DEFAULT 0",
        ),
    ),
    Migration(
        version=23,
        name="room_key_backup",
        # Server-side key backup (/room_keys): per-user backup versions plus the
        # encrypted megolm session keys stored under each version. Versions are
        # soft-deleted (deleted=1, key rows dropped) so numbers stay monotonic;
        # etag is an opaque counter bumped whenever a version's keys change.
        statements=(
            "CREATE TABLE IF NOT EXISTS room_key_versions ("
            " user_id TEXT NOT NULL,"
            " version BIGINT NOT NULL,"
            " algorithm TEXT NOT NULL,"
            " auth_data_json TEXT NOT NULL,"
            " etag BIGINT NOT NULL DEFAULT 0,"
            " deleted BIGINT NOT NULL DEFAULT 0,"
            " created_ts BIGINT NOT NULL,"
            " PRIMARY KEY (user_id, version)"
            ")",
            "CREATE TABLE IF NOT EXISTS room_key_backups ("
            " user_id TEXT NOT NULL,"
            " version BIGINT NOT NULL,"
            " room_id TEXT NOT NULL,"
            " session_id TEXT NOT NULL,"
            " first_message_index BIGINT NOT NULL,"
            " forwarded_count BIGINT NOT NULL,"
            " is_verified BIGINT NOT NULL,"
            " session_data_json TEXT NOT NULL,"
            " PRIMARY KEY (user_id, version, room_id, session_id)"
            ")",
        ),
    ),
    Migration(
        version=24,
        name="remote_media_cache",
        # Locally-cached copies of media fetched from other servers over federation,
        # so a repeated download is served from our own store instead of re-fetching.
        # cache_key is the namespaced MediaStore blob key (see media/service.py); it is
        # derived from a hash and prefixed so a remote server can never target a local
        # media id's blob.
        statements=(
            "CREATE TABLE IF NOT EXISTS remote_media_cache ("
            " origin_server TEXT NOT NULL,"
            " origin_media_id TEXT NOT NULL,"
            " cache_key TEXT NOT NULL,"
            " content_type TEXT NOT NULL,"
            " upload_name TEXT,"
            " size BIGINT NOT NULL,"
            " fetched_ts BIGINT NOT NULL,"
            " PRIMARY KEY (origin_server, origin_media_id)"
            ")",
        ),
    ),
    Migration(
        version=25,
        name="room_directory",
        # Local room aliases (#localpart:server_name -> room_id) and the per-room
        # "published in the public directory" flag. Aliases are local-only mappings
        # this server owns; the published flag defaults to private (absent row).
        statements=(
            "CREATE TABLE IF NOT EXISTS room_aliases ("
            " alias TEXT PRIMARY KEY,"
            " room_id TEXT NOT NULL,"
            " creator TEXT NOT NULL,"
            " created_ts BIGINT NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_room_aliases_room ON room_aliases (room_id)",
            "CREATE TABLE IF NOT EXISTS room_directory ("
            " room_id TEXT PRIMARY KEY,"
            " visibility TEXT NOT NULL"
            ")",
        ),
    ),
    Migration(
        version=26,
        name="pushers_and_notifications",
        # Mobile push delivery. `pushers` holds each user's registered push
        # targets (a phone's device token via a push gateway); uniqueness is
        # (user_id, app_id, pushkey) per the spec. `notifications` records every
        # event a user's push rules said to notify about, for GET /notifications
        # and unread bookkeeping (highlight flag stored so only=highlight filters).
        statements=(
            "CREATE TABLE IF NOT EXISTS pushers ("
            " user_id TEXT NOT NULL,"
            " app_id TEXT NOT NULL,"
            " pushkey TEXT NOT NULL,"
            " kind TEXT NOT NULL,"
            " app_display_name TEXT,"
            " device_display_name TEXT,"
            " profile_tag TEXT,"
            " lang TEXT,"
            " data_json TEXT NOT NULL,"
            " ts BIGINT NOT NULL,"
            " PRIMARY KEY (user_id, app_id, pushkey)"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_pushers_pushkey ON pushers (app_id, pushkey)",
            "CREATE TABLE IF NOT EXISTS notifications ("
            " user_id TEXT NOT NULL,"
            " event_id TEXT NOT NULL,"
            " room_id TEXT NOT NULL,"
            " actions_json TEXT NOT NULL,"
            " ts BIGINT NOT NULL,"
            " highlight BIGINT NOT NULL DEFAULT 0,"
            " PRIMARY KEY (user_id, event_id)"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_notifications_user_ts"
            " ON notifications (user_id, ts)",
        ),
    ),
    Migration(
        version=27,
        name="refresh_tokens",
        # Refreshable sessions (CS API v1.3 / Element X). Access tokens issued with
        # refresh support carry an expiry (`expires_at_ms`); classic tokens leave it
        # NULL and never expire. `refresh_tokens` are long-lived and single-use: on
        # refresh the old row is marked `used` and linked to its successor via
        # `next_token`, so a replayed (already-rotated) refresh token is rejected.
        statements=(
            "ALTER TABLE access_tokens ADD COLUMN expires_at_ms BIGINT",
            "CREATE TABLE IF NOT EXISTS refresh_tokens ("
            " token TEXT PRIMARY KEY,"
            " user_id TEXT NOT NULL,"
            " device_id TEXT NOT NULL,"
            " created_ts BIGINT NOT NULL,"
            " used BIGINT NOT NULL DEFAULT 0,"
            " next_token TEXT"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens (user_id)",
        ),
    ),
    Migration(
        version=28,
        name="federation_edu_outbox",
        # Durable queue for reliability-critical EDUs (m.direct_to_device carrying
        # Olm/Megolm key material, m.device_list_update). Mirrors federation_outbox
        # exactly — same lease model (owner + leased_until) so a worker claims a
        # destination's rows before sending and a crashed worker's lease expires.
        # A dropped to-device EDU means "unable to decrypt", so unlike receipts/typing
        # these must survive an offline peer. Ephemeral EDUs are never queued here.
        statements=(
            "CREATE TABLE IF NOT EXISTS federation_edu_outbox ("
            " stream_id BIGINT PRIMARY KEY,"
            " destination TEXT NOT NULL,"
            " edu_json TEXT NOT NULL,"
            " leased_until BIGINT NOT NULL DEFAULT 0,"
            " owner TEXT"
            ")",
            "CREATE INDEX IF NOT EXISTS idx_edu_outbox_destination"
            " ON federation_edu_outbox (destination, stream_id)",
            # Message-level dedup for inbound m.direct_to_device. Durable retry uses a
            # fresh txn_id per attempt, so a redelivered to-device EDU bypasses
            # transaction dedup; recording (origin, message_id) makes applying an Olm
            # message exactly-once (a redelivery short-circuits). message_id is opaque
            # and never logged.
            "CREATE TABLE IF NOT EXISTS received_to_device ("
            " origin TEXT NOT NULL,"
            " message_id TEXT NOT NULL,"
            " received_ts BIGINT NOT NULL,"
            " PRIMARY KEY (origin, message_id)"
            ")",
        ),
    ),
    Migration(
        version=29,
        name="federation_destinations",
        # Per-destination federation delivery health, surfaced on the console's
        # Federation page. One row per remote server we've sent a transaction to:
        # the last success/failure timestamps, a run of consecutive failures (reset
        # to 0 on any success), and a SHORT last error string (an exception class +
        # truncated message — never key material or event content). Written
        # best-effort by the sender, so a health-write failure never breaks delivery.
        statements=(
            "CREATE TABLE IF NOT EXISTS federation_destinations ("
            " destination TEXT PRIMARY KEY,"
            " last_success_ts BIGINT,"
            " last_failure_ts BIGINT,"
            " consecutive_failures BIGINT NOT NULL DEFAULT 0,"
            " last_error TEXT"
            ")",
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
        " version BIGINT PRIMARY KEY,"
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
                " VALUES (?, ?, ?) ON CONFLICT (version) DO NOTHING",
                (migration.version, migration.name, datetime.now(UTC).isoformat()),
            )
        newly_applied.append(migration.version)
    return newly_applied
