# Neuron — Progress

Tracks implementation progress against `PLAN.md`. Updated per phase.

---

## Phase 0 — Foundation & local dev environment — ✅ built

Project skeleton under `neuron/` (separate from Synapse's tree); the shared
`neuron_core` library (typed `SynapseAdminClient`, `pydantic-settings` config
with `SecretStr` token, stdlib JSON/console logging, error types); unit tests
(httpx `MockTransport`) + an auto-skipping integration smoke test; a Docker
Compose dev stack (stock Synapse + PostgreSQL + Redis); CI (ruff + mypy +
pytest, path-filtered to `neuron/**`); dev-setup script.

Verified locally: ruff clean, mypy clean, unit tests pass. Live `docker compose`
bring-up needs a Docker daemon (run it in your environment to fully close it).

---

## Phase 1 — neuron-console (read-only) — ✅ built

A FastAPI web UI over the Synapse Admin API: operator login (password + signed
session cookie; the admin token stays server-side and never reaches the
browser), and server-rendered pages for the dashboard, users (list/search/
detail), rooms (list/search/detail), content reports, and `/healthz`.

---

## Phase 2 — neuron-console (write) + auth-mode handling — ✅ built

Write actions: create/modify/deactivate users, reset password (classic auth),
shadow-ban, registration tokens, server notices, room block/delete (async +
status page), redaction (async + status page). CSRF protection on every form,
confirmation prompts for destructive actions, flash messages. Under
`NEURON_AUTH_MODE=mas`, endpoints Synapse disables are blocked with a clear
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
  live run (`run` against the dev Synapse + MinIO).

### Review gate
Run the auditor against the dev Synapse, send messages in a room the bot is in,
and confirm they land in `audit-log.jsonl` (and MinIO if configured), surviving a
restart. When happy, we proceed to **Phase 5 — E2EE for auditor & supervisor**
(the hard phase).

---
## Phase 5 + 5b — E2EE for auditor — ✅ built (offline-validated), awaiting review gate

**Goal:** read/audit **encrypted** room messages, including automatic key receipt.

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

### Verified locally (offline, with libolm)
- `ruff` clean, `mypy` clean (29 source files), `pytest` → **55 passed,
  3 skipped**. Includes the **full automatic pipeline end-to-end**: a sending
  device claims the bot's one-time key → sends an Olm to-device `m.room_key` →
  the manager ingests it → a Megolm room message then decrypts — plus Megolm
  round-trips, persistence, key-file import, `keys_upload` shape, and the auditor
  to-device wiring.

### Honest scope / what needs a live homeserver
- **Server-side validation:** `/keys/upload` acceptance and a *real* client
  choosing to share keys can only be exercised against a running Synapse + a
  cooperating client. The crypto is validated offline; the wire calls are
  unit-tested for shape.
- **Trust / verification:** well-behaved clients typically share keys only with
  **verified / cross-signed** devices. Cross-signing setup + verification is not
  implemented; the bot's device should be verified by the operator (or the
  deployment must allow sharing with it). This is the main operational
  prerequisite for automatic decryption in practice.
- **OTK replenishment:** a batch of one-time keys is published at startup;
  ongoing replenishment from sync's key counts is a refinement, not yet added.
- **Forward-only (protocol limit):** messages sent before the bot held the key
  can't be read unless imported. **Security:** the audit store is plaintext and
  device/key files can decrypt messages — all must be access-controlled.

### Review gate
Validate against the dev Synapse (or accept offline validation), then choose:
add **verification/cross-signing + OTK replenishment** to finish live E2EE, or
move to **Phase 6 — media scanner**.

---
## Phase 6 — neuron-mediascan (ClamAV) — ⬜ not started
## Phase 7 — neuron-directory (IAM / GroupSync) — ⬜ not started
## Phase 8 — neuron-gateway (federation firewall) — ⬜ not started
## Phase 9 — neuron-scale (HA blueprint) + hardening — ⬜ not started
