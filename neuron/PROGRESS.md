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

## HS-6 — Remaining CS API + Synapse-compatible Admin API — ✅ built (cut-over)

The milestone where the existing Neuron tooling runs against `neuron_server`.

- **Synapse-compatible Admin API** (`api/synapse_admin.py`, `admin/service.py`):
  the `/_synapse/admin/...` surface the console/bots use — `server_version`,
  users (list/get/create/modify with 201-on-create, deactivate, reset_password),
  rooms (list/get/members/state), registration tokens (list/new/delete),
  `make_room_admin` + force-`join` (server-authority, via `RoomService`), plus
  spec-shaped responses for shadow-ban / server-notice / room block-and-delete /
  redaction / event-reports. Admin auth via a token whose user is in
  `NEURON_SERVER_ADMIN_USERS` or has the DB admin flag.
- **Remaining CS API** (`api/client_misc.py`): profile (display name / avatar),
  global + room **account data**, **filters**, `capabilities` (advertises room
  version 11), a minimal push-rules ruleset, and accepted-but-stubbed
  presence/typing/receipts/read-markers.
- **Storage** (migration 0006): `profiles`, `account_data`, `filters`,
  `registration_tokens`; admin user queries in `storage/admin.py`.

Acceptance criterion met — **the existing Neuron stack runs against
`neuron_server`**: `neuron_core`'s `AdminClient` drives the admin API end to end
(users/rooms/tokens/make-room-admin; non-admin gets 403), and **the repo's
`neuron_core` integration smoke tests — written for Synapse — pass unchanged
against `neuron_server`** (verified by pointing `NEURON_HOMESERVER_URL` at a live
`neuron_server`). ruff + mypy clean (79 files); 116 unit tests pass.

Honest scope / deferred: shadow-ban / server-notice / async room-purge / redaction
jobs / content-reports are spec-shaped stubs (no backing work yet);
registration-token gating isn't enforced in the register flow; account data isn't
surfaced in `/sync` yet.

---

# 🎯 Non-federating MVP complete (HS-0 → HS-6)

`neuron_server` is now a usable, clean-room, **non-federating** Matrix homeserver:
identity & auth, rooms with spec-enforced authorization (room v11), `/sync`
(long-polling), media, E2EE key relay, the everyday CS API, and a
Synapse-compatible Admin API. The existing Neuron services (console, supervisor,
auditor) and `neuron_core` work against it. Built strictly from the open spec —
no homeserver source was ever consulted.

### Remaining homeserver work (separate epics)
- **HS-7 — Federation** (the hard, research-grade epic): server keys ✅, transactions
  (PDUs/EDUs), make/send join, backfill, **state resolution v2**, per-version auth
  rules, server ACLs. Months of work; its own program. **In progress** (below).
- **HS-8 — Parity, conformance & cut-over**: pass a growing subset of Complement;
  caching/perf; optional workers; then swap the dev stack + Neuron services from
  the transitional upstream backend to `neuron_server`.
- Backfill within the MVP: key backup (`/room_keys`), UIA on sensitive endpoints,
  per-membership history visibility, real shadow-ban/server-notice/purge jobs.

---

## HS-7 — Federation — 🚧 in progress

### Step 1 — Server signing keys + key publishing — ✅ built

The first brick of server-to-server identity, the prerequisite for every later
federation step (everything a server sends is signed with this key).

- **Crypto primitives** (`crypto/signing.py`): Matrix canonical JSON, unpadded
  base64, Ed25519 keys via libsodium/PyNaCl, the Synapse-compatible
  ``ed25519 <version> <base64-seed>`` key file format, and the spec's
  ``signatures`` envelope (`sign_json` / `verify_signed_json`, which exclude the
  `signatures` and `unsigned` members from the signed bytes).
- **Server identity** (`keys/service.py`): loads the Ed25519 signing key from a
  file (`NEURON_SERVER_SIGNING_KEY_PATH`, created on first run) or generates it
  once and persists it in `server_metadata`, so the federation identity is stable
  across restarts. Builds the signed key document.
- **Endpoint** (`api/federation_keys.py`): `GET /_matrix/key/v2/server`
  (+ the deprecated `/server/{keyId}` form) — the self-signed document of verify
  keys, `valid_until_ts` and `old_verify_keys`, unauthenticated by design.
- **Dependency:** added `PyNaCl` to the `server` extra (libsodium Ed25519).

Acceptance criterion met — **the published key verifies the way a remote
homeserver would**: a unit test (and a live `uvicorn` run cross-checked with raw
libsodium + an independent canonical-JSON reconstruction) fetches
`/_matrix/key/v2/server` and verifies the self-signature using only the published
verify key; the signing key is shown stable across restarts (DB and file
backends). Ed25519 wiring is cross-checked against libsodium; signing envelope has
roundtrip + tamper + multi-signature tests. ruff + mypy clean (84 files); 127
tests pass.

### Step 2 — Event hashing, reference-hash IDs & event signing — ✅ built

Our events are now proper, verifiable federation PDUs (and our room-v11 event IDs
are now spec-correct instead of opaque random strings).

- **Hashing/signing** (`crypto/event_hashing.py`): the v11 redaction algorithm
  (top-level allowlist + per-type content allowlist), the **content hash**
  (`hashes.sha256`), the **reference hash**, the room-v4+ **event ID**
  (`$` + URL-safe unpadded base64 of the reference hash), and event
  signing/verification (sign the redacted form, which carries the content hash, so
  the signature commits to the whole event). Content-hash and signature
  verification helpers included for future federation ingestion.
- **Auth-event selection** (`authrules.select_auth_event_ids`): the spec's
  algorithm picking the state events that authorise a new event (`m.room.create`
  is its own root; members pull in join rules / target membership / third-party
  invites / restricted-join authoriser).
- **Event building** (`rooms/service.py`): `_append` now selects `prev_events`
  (the forward extremity) and `auth_events`, computes the content hash, derives the
  reference-hash event ID, and signs the event with the server key before
  persisting it. Redaction events carry `redacts` in `content` (MSC2174). The full
  signed PDU is stored (migration 0007, `events.pdu_json`).

Acceptance criterion met: a service-level test drives `RoomService` against a real
database and shows that every stored event's **ID equals its reference hash**, its
**server signature verifies** with the published verify key, and its
`auth_events`/`prev_events` links are correct — i.e. the events would be acceptable
to a remote homeserver. Plus unit tests for the redaction algorithm, content-hash
tamper detection, signature/`unsigned` independence, and auth-event selection.
Found & fixed a real bug along the way (the reference hash must not include an
`event_id` field). ruff + mypy clean (85 files); 137 tests pass.

### Step 3 — Federation request auth (X-Matrix) + read endpoints — ✅ built

The inbound federation read surface, so a remote homeserver can fetch the signed
PDUs we produce — gated by proper server-to-server request authentication.

- **X-Matrix request auth** (`federation/auth.py`): signs/verifies the canonical
  JSON request description (`method`/`uri`/`origin`/`destination`/`content`) and
  builds/parses the `Authorization: X-Matrix origin=…,key=…,sig=…` header. Used for
  both inbound verification and (later) outbound signing.
- **Read endpoints** (`api/federation_read.py`): `GET /_matrix/federation/v1/version`
  (unauthenticated), and — behind X-Matrix auth + an origin-in-room check —
  `/event/{eventId}`, `/state/{roomId}` and `/state_ids/{roomId}`, returning the
  stored PDUs and their transitive **auth chain**.
- **Storage** (`storage/rooms.py`): `get_event_global`, `get_auth_chain`
  (auth-events closure).

Acceptance criterion met: a loopback test signs federation requests with the
server's own key, fetches an event and room state, and verifies the **served PDUs'
signatures the way a remote server would**; unauthenticated requests get 401 and
bad signatures 403. Plus X-Matrix sign/verify/parse unit tests (GET + body,
tamper, non-X-Matrix). ruff + mypy clean (88 files); 144 tests pass.

Honest scope / deferred: the origin's verify keys are resolved **locally only**
(remote-key resolution needs the outbound federation client), room state is the
**current** state (per-event historical state needs state groups), and there is no
content-hash/auth re-check of inbound events yet.

### Step 4 — Outbound federation client + remote key resolution — ✅ built

Federation is no longer loopback-only: we can now fetch and verify **real remote
servers'** keys and authenticate their requests.

- **Outbound client** (`federation/client.py`): signs outbound requests with
  X-Matrix and sends them; an `open_client` seam lets us route to an in-process
  server over an ASGI transport (used by tests) instead of the network.
- **Server discovery** (`federation/discovery.py`): `pick_base_url` — explicit port
  wins, else honour `/.well-known/matrix/server` delegation, else default to 8448.
- **Key resolution** (`keys/resolver.py`): `ServerKeyResolver` returns our own keys
  locally and, for a remote server, returns cached keys or fetches its
  `/_matrix/key/v2/server` document, **verifies it is correctly self-signed**
  (`parse_and_verify_key_document`), caches it (migration 0008,
  `remote_server_keys`), and returns the keys. Wired into the federation read
  auth, so inbound requests from real remote origins now authenticate.

Acceptance criterion met — **a two-server, in-process federation test**: server A
resolves server B's keys by fetching B's key document over an ASGI transport, the
keys are cached (resolution then works with the network removed), and a request B
signs to A authenticates (the only remaining failure is A's room-membership check,
proving auth itself passed); a forged/mismatched key document and a bad signature
are both rejected. Plus discovery unit tests. ruff + mypy clean (92 files); 147
tests pass.

Honest scope / deferred: `.well-known` delegation isn't fetched by the default
client yet (the decision function is in place); the server key **notary**
(`/_matrix/key/v2/query`) is deferred; no key-validity refresh/rotation handling
beyond `valid_until_ts`.

### Step 5 — Inbound transaction ingest + PDU validation — ✅ built

The federation **ingress security gate**: events arriving from other servers are
cryptographically validated before they could ever be trusted.

- **Validation pipeline** (`federation/validation.py`): checks a PDU's structure
  and size, recomputes its reference-hash **event ID**, verifies its **content
  hash**, and verifies a **signature from the sender's server** (keys resolved via
  the `ServerKeyResolver`). Returns the event ID or raises a peer-safe
  `PduValidationError`.
- **Transaction endpoint** (`api/federation_transactions.py`):
  `PUT /_matrix/federation/v1/send/{txnId}` — authenticates the request over its
  **signed body** (X-Matrix), checks the body origin matches, validates each PDU,
  and returns the spec's per-PDU result map (`{}` on success, `{"error": …}` on
  failure).
- **Shared federation auth** (`federation/request.py`): one `authenticate_request`
  helper (now body-aware for POST/PUT) used by both the read and transaction
  endpoints; `federation_read` was refactored onto it.

Acceptance criterion met — **a two-server transaction test**: server B builds a
genuine signed event and sends it to A in a transaction; A authenticates B
(resolving B's keys) and **validates the real PDU** (per-PDU result `{}`); a
tampered-content PDU is rejected with a `content hash` error, and an
unauthenticated transaction gets 401. Plus unit tests for every rejection path
(bad hash, bad signature, unresolvable sender, missing fields, sender-server
signature required). ruff + mypy clean (95 files); 154 tests pass.

Honest scope / deferred: an "accepted" PDU here means **cryptographically valid** —
durable **state application** (authorising the event against its `auth_events`,
persisting it into room state) is the next step and needs state resolution v2; EDUs
in the transaction are accepted but ignored for now.

### Step 6a — Federated join, resident side (make_join / send_join) — ✅ built

A **remote user can now join a room we host** over real federation — the first
end-to-end membership flow across two servers.

- **`make_join`** (`GET /_matrix/federation/v1/make_join/{roomId}/{userId}`):
  returns an unsigned join-event **template** (selected `auth_events`, the forward
  extremity as `prev_events`, depth, room version) for the remote server to
  complete. Refuses users not on the calling server, and rooms that aren't public
  and haven't invited them.
- **`send_join`** (`PUT /_matrix/federation/v2/send_join/{roomId}/{eventId}`):
  validates the remote server's signed join event (content hash + sender-server
  signature), **authorises it against current room state** and persists it, then
  returns the room's current **state** and **auth chain** (`RoomService.
  apply_external_join`).

Acceptance criterion met — **a two-server join test**: a user on server A joins a
public room hosted by server B; A fetches the template, completes and signs the
join with A's key, and sends it back; B authenticates A (resolving A's keys),
validates and applies the join, and afterwards **B's room lists the remote user as
joined**. make_join for a user not on the origin is refused. ruff + mypy clean (96
files); 155 tests pass.

Honest scope / deferred: the **joining (outbound) side** — building the join from
the template and storing the returned room state locally so *our* users can join
*remote* rooms — is the next sub-step (6b); state is returned without conflict
resolution (single resident server, no forks), so **state resolution v2** is still
to come, as is applying transaction-ingested events to room state.

Next steps in HS-7: the outbound join side (6b) so our users join remote rooms;
then **state resolution v2** + durable state application; then backfill and the
remaining federation read/EDU surface.
