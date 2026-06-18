# Neuron — Progress

Tracks implementation progress against `PLAN.md`. Updated per phase.

---

## Phase 0 — Foundation & local dev environment — ✅ built

Project skeleton under `neuron/`; the shared `neuron_core` library (typed
`AdminClient`, `pydantic-settings` config with `SecretStr` token, stdlib
JSON/console logging, error types); unit tests (httpx `MockTransport`) + an
auto-skipping integration smoke test; a Docker Compose dev stack (a transitional
backend homeserver — stock upstream image — + PostgreSQL + Redis); CI (ruff +
mypy + pytest, path-filtered to `neuron/**`); dev-setup script.

Verified locally: ruff clean, mypy clean, unit tests pass. Live `docker compose`
bring-up needs a Docker daemon (run it in your environment to fully close it).

---

## Phase 1 — neuron-console (read-only) — ✅ built

A FastAPI web UI over the homeserver Admin API: operator login (password + signed
session cookie; the admin token stays server-side and never reaches the
browser), and server-rendered pages for the dashboard, users (list/search/
detail), rooms (list/search/detail), content reports, and `/healthz`.

---

## Phase 2 — neuron-console (write) + auth-mode handling — ✅ built

Write actions: create/modify/deactivate users, reset password (classic auth),
shadow-ban, registration tokens, server notices, room block/delete (async +
status page), redaction (async + status page). CSRF protection on every form,
confirmation prompts for destructive actions, flash messages. Under
`NEURON_AUTH_MODE=mas`, endpoints the homeserver disables are blocked with a clear
message (full MAS/OIDC operator login deferred per the classic-first decision).

---

## Phase 3 — neuron-supervisor (plaintext moderation) — ✅ built

A privileged bot that promotes itself to room admin (Admin API
`make_room_admin`) and moderates: kick/ban as the bot (new `MatrixClient`,
Client-Server API), redact a user's messages (Admin API). A console Supervision
tab drives promote-all and per-room promote + per-member Kick/Ban. CLI:
`python -m neuron_supervisor sync|run`.

---

## Phase 4 — neuron-auditor (plaintext) — ✅ built, awaiting review gate

**Goal:** stream events from unencrypted rooms to durable outputs.

**Scope note:** plaintext only. The plan named matrix-nio for the bot; since
matrix-nio's value is E2EE (deferred to Phase 5), the plaintext auditor uses our
own lightweight `/sync` client (`MatrixClient`) — fewer dependencies, fully
testable. matrix-nio / decryption arrives in Phase 5.

### Delivered
- **`neuron_core` MatrixClient:** added `sync` (long-poll `/sync` with `since`),
  `join_room`, and `messages` (history pagination).
- **`neuron_auditor` package:**
  - `Auditor` — `poll_once()` (sync → auto-join invites → record timeline events
    → persist token) and `run_forever()` (resilient loop). Token persistence
    (`state.py`) gives **no gaps and no duplicates across restarts**.
  - Sinks (`sinks.py`): `FileSink` (JSON Lines), `S3Sink` (one object per event,
    S3/MinIO), `CompositeSink` ("both"), and `build_audit_record` — a **stable
    JSON envelope** (room/event/type/sender/ts/content + `encrypted`/`decrypted`
    flags). Encrypted events are **recorded as undecryptable envelopes, not
    dropped**.
  - `AuditorSettings` (bot token, sink choice, file path, S3 config, state path).
  - `python -m neuron_auditor run|once` CLI.
- **Config:** `auditor` extra (boto3 for S3); `NEURON_AUDITOR_*` settings.
  `.gitignore` excludes the state file and local audit log.

### Verified locally
- `ruff` clean, `mypy` clean (24 source files), `pytest` → **43 passed,
  3 skipped**. New tests: `sync`/`join_room`, the record schema (incl. encrypted),
  `FileSink` JSONL, `S3Sink` (fake client → correct bucket/key/body), and the
  `Auditor` loop (records + token persisted; restart resumes from token;
  auto-join invites; encrypted event recorded not dropped). CLI runs.

### Phase 4 acceptance criteria status
- [✅] Events appear as JSON in the filesystem sink and S3, no gaps across a
  restart — covered by unit tests (file/S3 sinks + token-resume); ready for a
  live run (`run` against the dev homeserver + MinIO).

### Review gate
Run the auditor against the dev homeserver, send messages in a room the bot is in,
and confirm they land in `audit-log.jsonl` (and MinIO if configured), surviving a
restart. When happy, we proceed to **Phase 5 — E2EE for auditor & supervisor**
(the hard phase).

---
## Phase 5 + 5b + 5c — E2EE for auditor — ✅ built (offline-validated), awaiting review gate

**Goal:** read/audit **encrypted** room messages, including automatic key receipt,
cross-signing identity, and one-time-key replenishment.

### Delivered — Phase 5 (Megolm decryption core)
- **`neuron_crypto` package:** `base.py` (no libolm dependency) with the
  `Decryptor` protocol, `DecryptResult`, `NullDecryptor`; `megolm.py` (needs
  libolm via the `e2e` extra) with `MegolmSessionStore` (import keys from an
  `m.room_key` content or a JSON key file; pickle persistence) and
  `MegolmDecryptor`, decrypting `m.room.encrypted` (`m.megolm.v1.aes-sha2`) to the
  inner cleartext.
- **Auditor integration:** optional `decryptor`; encrypted events are decrypted
  when a key is available (inner type/content + `decrypted: true`) else recorded
  as an **undecryptable envelope** with a `decryption_error` reason — never dropped.

### Delivered — Phase 5b (automatic key receipt)
- **`OlmDevice`** (`olm_device.py`): the bot's Olm identity — device + one-time
  keys (signed, for `/keys/upload`), Olm to-device decryption, account/session
  persistence.
- **`E2EEManager`** (`manager.py`): `handle_to_device(events)` decrypts Olm
  to-device messages and imports the Megolm key from any `m.room_key`; `decrypt()`
  then reads room events — so once a room's key is received, its messages decrypt
  automatically.
- **`MatrixClient.keys_upload`** to publish device + one-time keys.
- **Auditor** now feeds each sync's `to_device` events to the decryptor before
  recording, so keys are ingested automatically. Full E2EE mode is enabled by
  `NEURON_AUDITOR_E2E_DEVICE_STORE` (persistent device; publishes keys on startup).

### Delivered — Phase 5c (trust + key lifecycle)
- **Cross-signing** (`cross_signing.py`): `CrossSigning` generates the master /
  self-signing / user-signing keys, builds the signed
  `keys/device_signing/upload` body, and **self-signs the bot's device** for
  `keys/signatures/upload`; seeds persist. `MatrixClient.upload_cross_signing_keys`
  + `upload_signatures` (the upload usually needs interactive auth — handled
  gracefully). Enabled by `NEURON_AUDITOR_E2E_CROSS_SIGNING`.
- **One-time-key replenishment:** `E2EEManager.maybe_generate_one_time_keys`
  tops keys up when the server reports them low; the auditor calls it each sync
  and re-uploads. Received room keys + Olm sessions are now **persisted** as they
  arrive (so a restart keeps them).

### Verified locally (offline, with libolm)
- `ruff` clean, `mypy` clean (31 source files), `pytest` → **59 passed,
  3 skipped**. Includes the **full automatic pipeline end-to-end** (claim OTK →
  Olm to-device `m.room_key` → ingest → Megolm message decrypts), Megolm
  round-trips + persistence, **cross-signing signatures verified with
  `olm.ed25519_verify`** (master→subkeys, self-signing→device), OTK replenishment
  logic, `keys_upload` shape, and the auditor to-device wiring.

### Honest scope / what still needs a live homeserver
- **Server-side + trust handshake:** `/keys/upload` and
  `/keys/device_signing/upload` acceptance (the latter typically needs UIA), and a
  *real* client *choosing* to share keys with the bot, can only be exercised
  against a running homeserver + a cooperating, trusting client. The crypto and
  payloads are validated offline; the wire calls are unit-tested for shape.
- **Verification is identity, not yet interactive trust:** the bot now publishes a
  proper cross-signed identity, but interactive (SAS) verification and a
  client/policy that shares with it remain operational. Forward-only still applies.
- **Security:** the audit store is plaintext; the device store, megolm session
  store, and cross-signing seeds can all decrypt messages — protect them.

### Review gate
Validate against the dev homeserver (or accept the offline validation), then move to
**Phase 6 — media scanner**.

---
## Phase 6 — neuron-mediascan (ClamAV) — ⬜ not started
## Phase 7 — neuron-directory (IAM / GroupSync) — ⬜ not started
## Phase 8 — neuron-gateway (federation firewall) — ⬜ not started
## Phase 9 — neuron-scale (HA blueprint) + hardening — ⬜ not started

---

# Homeserver — `neuron_server` (clean-room, see `HOMESERVER-PLAN.md`)

Neuron's own Matrix homeserver, built strictly from the open Matrix spec/MSCs
(never from another server's source). Replaces the transitional upstream backend
once it reaches parity (HS-6). Milestone order: HS-0..HS-6 (non-federating MVP),
then HS-7 (federation) as a separate epic.

## HS-0 — Foundation & spec harness — ✅ built

New package `neuron_server` (`pip install -e ".[server]"`): a FastAPI/ASGI
skeleton; an async storage layer (`storage/`) with a backend-agnostic `Database`
interface — **SQLite** (`aiosqlite`, dev) and **PostgreSQL** (`asyncpg`, prod) —
and an idempotent migration runner (`schema_migrations`); the spec-discovery
endpoints `GET /_matrix/client/versions` and `GET /.well-known/matrix/client`,
a `/health` probe, and a spec-correct `M_UNRECOGNIZED` catch-all for unknown
`/_matrix` requests. On startup the server records its `server_name` in
`server_metadata` and refuses to start if a database is later pointed at a
different name. Run with `python -m neuron_server`.

Acceptance criterion met: a client sees a valid Matrix server (`/versions`,
`.well-known`) and DB migrations run. Verified live (uvicorn on a temp SQLite DB:
discovery endpoints serve, migrations applied, identity persisted) plus unit
tests (`tests/neuron_server/`). ruff + mypy clean; 67 unit tests pass.

Honest scope: HS-0 is foundation only — no auth, rooms, sync, media or E2EE yet
(those are HS-1..HS-5). `/versions` advertises *target* spec compatibility; real
clients can't log in until HS-1+. Single DB connection (pooling is HS-8). The
`asyncpg` path is implemented but its live exercise waits for a Postgres run; the
SQLite path is fully tested.

## HS-1 — Identity & auth — ✅ built

Local accounts on `neuron_server`: registration, login, logout, `whoami`, and
device management, built from the Client-Server API.

- **Storage** (migration 0002): `users` (PBKDF2-SHA256 password hashes),
  `devices`, `access_tokens`. Data-access in `storage/accounts.py`.
- **Domain** (`auth/`): `AuthService` (register/login/logout/lookup-token/device
  ops), stdlib PBKDF2 password hashing (`passwords.py`), ID/localpart helpers
  (`ids.py`), and an in-memory UIA session store (`uia.py`).
- **Endpoints** (`api/client_auth.py`): `POST /register` (UIA `m.login.dummy`
  flow, `inhibit_login`, `kind=guest` rejected, `registration_enabled` gate),
  `GET /register/available`, `GET`+`POST /login` (`m.login.password`),
  `POST /logout` + `/logout/all`, `GET /account/whoami`, and
  `GET/PUT/DELETE /devices[/{id}]`. A bearer-token dependency resolves tokens and
  returns `M_MISSING_TOKEN` / `M_UNKNOWN_TOKEN`.

Acceptance criterion met: you can register + log in via our server, **and
`neuron_core`'s `MatrixClient` authenticates against it** (`whoami` returns the
right user) — verified by an in-process ASGI test plus a live uvicorn run
(register → login → whoami → devices → bad-password 403). ruff + mypy clean;
80 unit tests pass.

Honest scope / simplifications: open registration defaults **on** (gate it in
prod); device update/delete are token-authenticated only (the spec also gates
device deletion behind UIA — added with the broader UIA work later); no rate
limiting, password-policy, 3PID, SSO/MAS, or guest accounts yet; UIA sessions are
in-memory (don't survive restart).

## HS-2 — Rooms, events & auth rules — ✅ core built

The heart of the homeserver: rooms with spec-enforced authorization, on room
version 11.

- **Storage** (migration 0003): `rooms`, `events` (with `stream_ordering` +
  `depth`), `current_state`, `room_memberships`, `event_txns` (idempotency).
  Data-access in `storage/rooms.py`.
- **Event model** (`rooms/events.py`): `Event` dataclass + client rendering;
  opaque `$…` event IDs (federation-grade reference-hash IDs deferred to HS-7 —
  IDs are opaque to clients). Redaction algorithm (`rooms/versions.py`).
- **Auth rules** (`rooms/authrules.py`): the spec's authorization rules for
  create, every membership transition (join/invite/leave/kick/ban with
  join-rule + power-level checks), power-level changes, and the generic
  power-level gate for all other events. Single-server, so current state *is* the
  auth context (no state resolution).
- **Domain** (`rooms/service.py`): `createRoom` (presets, power-level defaults,
  name/topic/invites/initial_state), send message/state events, membership ops,
  redaction (applies the redaction algorithm + `unsigned.redacted_because`), and
  reads.
- **Endpoints** (`api/client_rooms.py`): `POST /createRoom`;
  `PUT …/send/{type}/{txn}`; `PUT/GET …/state/{type}[/{key}]`;
  `POST …/{join,leave,invite,kick,ban,unban}` and `POST /join/{roomId}`;
  `PUT …/redact/{eventId}/{txn}`; `GET …/state`, `…/event/{id}`,
  `…/messages` (v3 **and** v1, paginated), `…/joined_members`,
  `GET /joined_rooms`.

Acceptance criterion met: rooms enforce the spec's auth rules and messages
send/read correctly — verified by 11 unit tests (create/state/membership/power
levels/kick/ban/redaction/idempotency) plus a live uvicorn run (power-level
denial returned the right 403) and a `neuron_core` `MatrixClient.joined_rooms()`
compat test. ruff + mypy clean; 92 unit tests pass.

Honest scope / deferred: room aliases & directory, knock/restricted joins,
guests, third-party invites, room upgrades, `/messages` filtering, and
federation-grade event hashing/signing + state resolution (HS-7). Power-levels
change validation uses the well-known "can't set/change a level above your own"
approximation rather than the full per-key delta rules.

## HS-3 — Sync — ✅ built

`GET /sync` over the event stream, with long-polling.

- **Tokens** are the server-local `stream_ordering` position. Initial sync (no
  `since`) returns each joined room's current state + a recent timeline slice;
  incremental sync returns events after the token. Invited rooms appear with
  stripped `invite_state`; recent leaves/bans appear under `leave`.
- **Long-poll** (`sync/notifier.py`): a `StreamNotifier` wakes waiting syncs when
  any event is appended (`RoomService` calls it post-commit), so an incremental
  sync with a `timeout` blocks until something changes instead of busy-polling.
- **Concurrency safety:** introduced an async lock around DB `transaction()` so
  concurrent writes on the single connection can't interleave `BEGIN`/`COMMIT`.
- **Endpoint** (`api/client_sync.py`): `GET /v3/sync` (`since`, `timeout`).
  `to_device` / `device_lists` / `account_data` are present but empty until HS-5.

Acceptance criterion met: a client syncs and sees live messages, **and the real
`neuron_auditor` records a message synced from `neuron_server`** (compat test) —
plus initial/incremental/invite/leave unit tests and a **concurrent long-poll**
test (a waiting sync wakes when another request sends). Verified live via uvicorn.
ruff + mypy clean (63 files); 98 unit tests pass.

Honest scope / deferred: per-membership history visibility (currently "shared"),
sync filters, `full_state`, presence/ephemeral/account-data payloads, and
gappy-sync state catch-up. The notifier wakes all waiters on any event (fine for
single-server); a smarter per-user/room notifier is a later optimization.

## HS-4 — Media repository — ✅ built

Authenticated media upload/download/thumbnail/config.

- **Blob store** (`media/store.py`): a `MediaStore` interface with a filesystem
  backend (sharded by media-ID prefix; disk I/O off the event loop via
  `asyncio.to_thread`). S3 backend can slot in later.
- **Metadata** (migration 0004, `storage/media.py`): the `media` table
  (content type, name, size, uploader).
- **Service** (`media/service.py`): upload (size-gated → `mxc://` URI),
  download (local only — remote/federated media is HS-7), thumbnail (Pillow,
  scale/crop, falls back to the original for non-images), config. Media IDs are
  validated against a strict charset to prevent path traversal. Downloads set a
  safe `Content-Disposition` (`inline` only for image/audio/video, else
  `attachment`) to avoid content-sniffing XSS.
- **Endpoints** (`api/client_media.py`): `POST /_matrix/media/v3/upload`;
  config, download and thumbnail under both the newer authenticated
  `/_matrix/client/v1/media/...` paths and the legacy `/_matrix/media/v3/...`
  paths. **All require an access token** (we don't serve unauthenticated media).

Acceptance criterion met: media round-trips — verified by 7 unit tests
(upload/download byte-match, auth required, config, thumbnail resize, unknown →
404, remote → 404, oversize → 413) and a live uvicorn run (upload → download
byte-identical, 16×16 thumbnail, 401 without a token). ruff + mypy clean (69
files); 105 unit tests pass.

Honest scope / deferred: remote (federated) media (HS-7), async upload
(`/media/v1/create` + `PUT upload`), `preview_url`, per-media quotas/retention,
and the S3 blob backend (filesystem only for now).

## HS-5 — E2EE server support — ✅ built

The key-distribution side of E2EE — **store and relay only; the server never
decrypts**.

- **Storage** (migration 0005, `storage/e2ee.py`): `device_keys`,
  `one_time_keys`, `fallback_keys`, `cross_signing_keys`, `to_device_messages`,
  `device_list_changes` (stream IDs assigned `MAX+1` for portability).
- **Service** (`e2ee/service.py`): `keys/upload` (device keys + OTKs + fallback),
  `keys/query` (device + cross-signing keys), `keys/claim` (atomically consumes
  one OTK, falls back to the fallback key), `device_signing/upload` (cross-signing
  master/self/user-signing), `signatures/upload` (merges signatures into device /
  cross-signing keys), and `sendToDevice` (relays Olm-encrypted to-device events,
  expanding `*` to all of a user's devices).
- **Sync integration:** the sync token is now composite
  (`events.to_device.device_list`); `/sync` delivers pending to-device messages
  (with ack-based cleanup once the client advances its token), reports
  `device_one_time_keys_count`, and reports `device_lists.changed` for users
  sharing a room with the syncer. to-device/keys changes wake long-polling syncs.
- **Endpoints** (`api/client_keys.py`): `keys/upload`, `keys/query`, `keys/claim`,
  `keys/device_signing/upload`, `keys/signatures/upload`, `sendToDevice`.

Acceptance criterion met — **automatic key receipt works end to end against our
server**: a real-libolm pipeline test claims an OTK, shares a Megolm key via an
Olm `sendToDevice`, and the recipient **syncs against `neuron_server`**, decrypts
the to-device message, imports the room key, and decrypts the room message. Plus
relay unit tests (upload/query/claim/counts, sendToDevice delivery + ack,
cross-signing) and a live uvicorn run. ruff + mypy clean (73 files); 110 tests
pass.

Honest scope / deferred: server-side **key backup** (`/room_keys` + versions) is a
follow-up; `device_signing/upload` does not enforce UIA yet; signature merging is
best-effort for cross-signing keys; `device_lists.left` is always empty.

### Next gate
HS-6 — remaining CS API (account data, profiles, filters, capabilities,
typing/receipts/presence stubs, push-rules stubs) **plus a Synapse-compatible
`/_synapse/admin/...` surface**, so the existing Neuron console & bots run against
`neuron_server`. The cut-over milestone.
