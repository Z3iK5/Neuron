# Neuron

**Neuron** is an independent, clean-room Matrix platform: an all-in-one,
self-owned product that bundles operator tooling (an admin console, a
supervision bot, an audit bot, E2EE crypto, and a high-availability deployment
blueprint) and is growing its **own** Matrix homeserver (`neuron_server`, see
[`HOMESERVER-PLAN.md`](../HOMESERVER-PLAN.md)).

Until `neuron_server` reaches parity, Neuron's services run against a
**transitional backend homeserver** — a stock, unmodified upstream image used as
an opaque black box (see `deploy/compose/`). Neuron talks to it only over
**public Matrix APIs** (Client-Server, Server-Server, Application Service) and
the **homeserver Admin API**. Neuron contains no homeserver source, forks or
patches nothing, and is not derived from any other homeserver (see the
repository `NOTICE`).

> **Design docs:** see the repository root for `ARCHITECTURE.md`,
> `FEATURE-MATRIX.md`, `PLAN.md`, `OPEN-QUESTIONS.md`, and `HOMESERVER-PLAN.md`,
> plus the cited behavioral research in `docs/feature-analysis.md`.

## Status

Early development. Built phase-by-phase per `PLAN.md`; current progress is
tracked in [`PROGRESS.md`](./PROGRESS.md).

- **Phase 0 — Foundation**: project skeleton, the shared `neuron_core` library,
  a local dev backend homeserver via Docker Compose, and CI.
- **Phase 1 — Admin console (read-only)**: `neuron_console`, a FastAPI web UI
  over the homeserver Admin API for browsing users, rooms, and content reports.
- **Phase 2 — Admin console (write)**: user/room/token management with CSRF and
  MAS-aware guards.
- **Phase 3 — Supervision bot**: `neuron_supervisor` promotes a bot to room
  admin and moderates (kick/ban/redact).
- **Phase 4 — Audit bot**: `neuron_auditor` streams room events to filesystem/S3.
- **Phase 5 — E2EE (crypto core)**: `neuron_crypto` decrypts Megolm messages so
  the audit bot can read encrypted rooms when it holds the keys.

## Repository layout

```
neuron/
├── src/neuron_core/     # shared library: homeserver Admin API client, config, logging
├── tests/               # unit tests (and integration tests that need a live homeserver)
├── deploy/compose/      # local dev stack: backend homeserver + PostgreSQL (+ Redis)
├── scripts/             # helper scripts (dev setup, etc.)
├── pyproject.toml       # Neuron's own Python project
└── README.md
```

## Quick start (local development)

You need Python 3.11+ and (for the live homeserver) Docker.

```bash
cd neuron

# 1. Create a virtualenv and install Neuron with its dev tools.
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

# 2. Run the linters, type checker, and unit tests (no homeserver needed).
ruff check .
mypy
pytest

# 3. (Optional) Bring up a local dev backend homeserver to test against.
#    See deploy/compose/README.md for the full walkthrough.
cd deploy/compose
cp .env.example .env        # then edit values as needed (never commit .env)
docker compose up -d
```

### Run the admin console (Phases 1–2)

With a homeserver to talk to (see `deploy/compose/README.md` for getting an admin
token), from the `neuron/` directory with the venv active:

```bash
export NEURON_HOMESERVER_URL=http://localhost:8008
export NEURON_HOMESERVER_ADMIN_TOKEN=<server-admin token>
export NEURON_SERVER_NAME=neuron.local         # needed to create users by username
export NEURON_CONSOLE_PASSWORD=<a password you choose>  # to log in to the console
# Optional: NEURON_AUTH_MODE=mas  if your homeserver delegates auth to MAS (MSC3861)
uvicorn neuron_console.app:app --reload --port 8080
# then open http://localhost:8080
```

The console never exposes the admin token to the browser. It supports browsing
**and** write actions (create/modify/deactivate users, reset passwords,
shadow-ban, registration tokens, server notices, room block/delete, redaction),
with CSRF protection and confirmation prompts for destructive actions. Under
`NEURON_AUTH_MODE=mas`, actions the homeserver disables (e.g. password reset) are
blocked with an explanatory message.

### Supervision bot (Phase 3)

`neuron_supervisor` is a privileged bot that keeps itself promoted to room-admin
(via the Admin API's `make_room_admin`) so any room can always be moderated, and
performs kick/ban as the bot. First create a **local** bot account and get its
access token, then configure:

```bash
export NEURON_SUPERVISOR_BOT_USER_ID=@supervisor:neuron.local
export NEURON_SUPERVISOR_BOT_TOKEN=<the bot account's access token>
```

Run promotion once, or as a background poll loop:

```bash
python -m neuron_supervisor sync    # promote the bot in all rooms once
python -m neuron_supervisor run     # keep re-promoting on a timer
```

With the same two env vars set for the console, its **Supervision** tab can
promote the bot and the per-room member list gains **Kick/Ban** buttons.
(Reading/moderating *encrypted* rooms needs E2EE, which comes in a later phase.)

### Audit bot (Phase 4)

`neuron_auditor` joins rooms and streams every event to a durable sink (local
JSON Lines and/or an S3-compatible bucket). Create a local audit-bot account, get
its token, then:

```bash
export NEURON_AUDITOR_BOT_TOKEN=<the audit bot account's access token>
export NEURON_AUDITOR_SINK=file              # file | s3 | both
export NEURON_AUDITOR_FILE_PATH=audit-log.jsonl
python -m neuron_auditor run                  # stream until stopped (Ctrl+C)
```

Invite the bot to a room (or let it auto-join), send messages, and watch them
appear as JSON lines in `audit-log.jsonl`. A resume token is persisted, so
restarting the bot continues without gaps or duplicates. For S3/MinIO, set
`NEURON_AUDITOR_SINK=s3` (or `both`) and the `NEURON_AUDITOR_S3_*` variables.

### End-to-end encryption (Phase 5)

The auditor can decrypt **encrypted** rooms. Install the E2EE extra (needs system
`libolm`, e.g. `apt-get install libolm-dev`): `pip install -e ".[e2e]"`. Two modes:

**Automatic (recommended)** — the bot gets a persistent Olm device, publishes its
keys on startup, and ingests room keys sent to it via to-device messages:

```bash
export NEURON_AUDITOR_E2E_DEVICE_STORE=/path/to/auditor-device.json
export NEURON_AUDITOR_E2E_CROSS_SIGNING=true   # publish a cross-signed identity (optional)
python -m neuron_auditor run
```

In this mode the bot also **replenishes its one-time keys** automatically as they
are consumed, and (with cross-signing on) publishes master/self-signing/user-signing
keys and self-signs its device so it presents a verifiable identity.

**Import-only** — decrypt rooms whose Megolm keys you provide in a file:

```bash
export NEURON_AUDITOR_E2E_KEY_FILE=/path/to/room-keys.json   # JSON array of {session_key}
python -m neuron_auditor run
```

Events the bot can decrypt are recorded with their cleartext type/content and
`"decrypted": true`; events it can't are recorded as envelopes with a
`decryption_error` reason — **never dropped**.

> **Honest limits.** Decryption is *forward-only* (messages sent before the bot
> held the key can't be read). The bot can publish a cross-signed identity, but
> for automatic mode to work in practice a sending client must still *choose* to
> share keys with it — typically after its device is **verified** (interactive
> verification and the share decision are the sender's, and need a live setup).
> Uploading cross-signing keys usually needs interactive auth. The device store,
> session store, cross-signing seeds, key file, and the audit store (plaintext)
> can all expose message content — protect them like the secrets they are.

### Neuron homeserver — `neuron_server` (HS-0, in progress)

Neuron has its **own** clean-room Matrix homeserver (see
[`HOMESERVER-PLAN.md`](../HOMESERVER-PLAN.md)) so the project is one self-owned,
all-in-one product instead of services around someone else's server. It is built
strictly from the open Matrix spec/MSCs.

The **non-federating MVP is complete** (HS-0 → HS-6): identity & auth, rooms with
the spec's authorization rules (room v11), `GET /sync` (long-polling), a media
repository, E2EE key relay (store/relay only — the server never decrypts), the
everyday Client-Server API (profile, account data, filters, capabilities), and a
**Synapse-compatible `/_synapse/admin/...` Admin API**. `neuron_core`, the admin
console and the bots run against it unchanged. Federation is a separate later epic
(HS-7). Run it with the `server` extra (no Docker needed; defaults to a local
SQLite file):

```bash
pip install -e ".[server]"
export NEURON_SERVER_NAME=neuron.local
export NEURON_SERVER_PUBLIC_BASE_URL=http://localhost:8008
export NEURON_SERVER_DATABASE_URL=sqlite:///./neuron_server.db   # or postgresql://…
python -m neuron_server                      # serves on 127.0.0.1:8008

# Register a user (UIA m.login.dummy flow), then check who you are:
S=$(curl -s -XPOST localhost:8008/_matrix/client/v3/register \
     -d '{"username":"alice","password":"choose-a-password"}' | jq -r .session)
curl -s -XPOST localhost:8008/_matrix/client/v3/register \
  -d "{\"username\":\"alice\",\"password\":\"choose-a-password\",\"auth\":{\"type\":\"m.login.dummy\",\"session\":\"$S\"}}"
```

To run the Neuron console/bots against it, set `NEURON_SERVER_ADMIN_USERS` to your
admin's localpart, register that user, and point `NEURON_HOMESERVER_URL` /
`NEURON_HOMESERVER_ADMIN_TOKEN` at the server.

### Run it as a desktop app

For a personal/LAN server with no manual config, install the `desktop` extra and
let the first-run wizard pick a server name, create your admin account, and keep
all state in a per-user data directory (override with `NEURON_DATA_DIR`):

```bash
pipx install "neuron[desktop]"   # or: pip install -e ".[desktop]"
neuron-desktop                   # first run: setup wizard, then starts the server
neuron-desktop where             # show the data directory
neuron-desktop console           # open the admin console in a browser
```

State (database, media, signing key, config) lives in the OS app-data dir — macOS
`~/Library/Application Support/Neuron`, Windows `%LOCALAPPDATA%\Neuron`, Linux
`~/.local/share/Neuron`. A cross-platform tray app and native installers are the
next desktop phases (see `DESKTOP-PLAN.md`).

> **Honest status.** Server-to-server **federation is in progress** (HS-7): signed
> events, the key API, X-Matrix request auth, remote key resolution, transaction
> ingest and **bidirectional federated joins** work across two servers, with state
> resolution v2 implemented as a provisional library; backfill, EDUs and full
> conformance are still to come. Some Admin endpoints (shadow-ban, server notices,
> async purge/redaction jobs, content reports) are spec-shaped stubs. Registration
> defaults to **open** (gate `NEURON_SERVER_REGISTRATION_ENABLED` in production).

## Configuration & secrets

All configuration is read from environment variables prefixed with `NEURON_`
(see `src/neuron_core/config.py`). **Secrets never live in the repository**: copy
the provided `.env.example` files to `.env` (which is git-ignored) for local
development, and inject real values via your orchestrator's secret mechanism in
production.

## License

Neuron is licensed under the **Apache License, Version 2.0** (see the `LICENSE`
and `NOTICE` files at the repository root). It is an independent, clean-room
implementation built only from public documentation and the open Matrix
specification — it contains no third-party homeserver source and no Element
proprietary code.
