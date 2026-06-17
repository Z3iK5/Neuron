# Neuron

**Neuron** is a suite of independent, clean-room services that run *around* a
stock, unmodified [Synapse](https://github.com/element-hq/synapse) homeserver
(this repository) to provide the kind of functionality that distinguishes
commercial Matrix distributions: a federation firewall, identity/directory
sync, an audit bot, a supervision bot, an admin console, a media scanner, and a
high-availability deployment blueprint.

> **Design docs:** see the repository root for `ARCHITECTURE.md`,
> `FEATURE-MATRIX.md`, `PLAN.md`, and `OPEN-QUESTIONS.md`, plus the cited
> behavioral research in `docs/feature-analysis.md`.

Neuron talks to Synapse only over **public Matrix APIs** (Client-Server,
Server-Server, Application Service) and the **open Synapse Admin API**. It does
not fork or patch Synapse, and it contains no proprietary code.

## Status

Early development. Built phase-by-phase per `PLAN.md`; current progress is
tracked in [`PROGRESS.md`](./PROGRESS.md).

- **Phase 0 — Foundation**: project skeleton, the shared `neuron_core` library,
  a local dev Synapse via Docker Compose, and CI.
- **Phase 1 — Admin console (read-only)**: `neuron_console`, a FastAPI web UI
  over the Synapse Admin API for browsing users, rooms, and content reports.

## Repository layout

```
neuron/
├── src/neuron_core/     # shared library: Synapse Admin API client, config, logging
├── tests/               # unit tests (and integration tests that need a live Synapse)
├── deploy/compose/      # local dev stack: Synapse + PostgreSQL (+ Redis)
├── scripts/             # helper scripts (dev setup, etc.)
├── pyproject.toml       # Neuron's own Python project (separate from Synapse's)
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

# 2. Run the linters, type checker, and unit tests (no Synapse needed).
ruff check .
mypy
pytest

# 3. (Optional) Bring up a local dev Synapse to test against.
#    See deploy/compose/README.md for the full walkthrough.
cd deploy/compose
cp .env.example .env        # then edit values as needed (never commit .env)
docker compose up -d
```

### Run the admin console (Phase 1)

With a Synapse to talk to (see `deploy/compose/README.md` for getting an admin
token), from the `neuron/` directory with the venv active:

```bash
export NEURON_SYNAPSE_BASE_URL=http://localhost:8008
export NEURON_SYNAPSE_ADMIN_TOKEN=<server-admin token>
export NEURON_SYNAPSE_SERVER_NAME=neuron.local         # needed to create users by username
export NEURON_CONSOLE_PASSWORD=<a password you choose>  # to log in to the console
# Optional: NEURON_AUTH_MODE=mas  if your homeserver delegates auth to MAS (MSC3861)
uvicorn neuron_console.app:app --reload --port 8080
# then open http://localhost:8080
```

The console never exposes the admin token to the browser. It supports browsing
**and** write actions (create/modify/deactivate users, reset passwords,
shadow-ban, registration tokens, server notices, room block/delete, redaction),
with CSRF protection and confirmation prompts for destructive actions. Under
`NEURON_AUTH_MODE=mas`, actions Synapse disables (e.g. password reset) are
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

## Configuration & secrets

All configuration is read from environment variables prefixed with `NEURON_`
(see `src/neuron_core/config.py`). **Secrets never live in the repository**: copy
the provided `.env.example` files to `.env` (which is git-ignored) for local
development, and inject real values via your orchestrator's secret mechanism in
production.

## License

The Neuron code in this directory is licensed under **AGPL-3.0-or-later**,
consistent with the surrounding Synapse codebase and the wider Matrix ecosystem.
