# Neuron — Progress

Tracks implementation progress against `PLAN.md`. Updated per phase.

---

## Phase 0 — Foundation & local dev environment — ✅ built, awaiting review gate

**Goal:** a working monorepo skeleton and a one-command local Synapse to test against.

### Delivered
- **Project skeleton** under `neuron/` (kept fully separate from Synapse's tree):
  `src/neuron_core/`, `tests/`, `deploy/compose/`, `scripts/`, plus Neuron's own
  `pyproject.toml` (independent from Synapse's packaging).
- **`neuron_core` v0 — shared library:**
  - `config.py` — `NeuronCoreSettings`, loaded from `NEURON_*` env vars via
    `pydantic-settings`; admin token held as a `SecretStr`.
  - `logging.py` — `configure_logging` / `get_logger`; JSON or console output,
    standard-library only.
  - `errors.py` — `NeuronError`, `SynapseAdminError` (carries HTTP status +
    Matrix `errcode`).
  - `admin_client.py` — async `SynapseAdminClient` for the open Synapse Admin
    API; Phase 0 read endpoints: `get_server_version`, `list_users`, `get_user`.
- **Tests:**
  - Unit tests (no server needed) for config and the admin client (using httpx's
    `MockTransport`): `tests/neuron_core/`.
  - Integration smoke test `tests/integration/test_smoke.py` — talks to a live
    Synapse; auto-skips unless `NEURON_SYNAPSE_BASE_URL` + `NEURON_SYNAPSE_ADMIN_TOKEN`
    are set.
- **Local dev stack** `deploy/compose/` — stock Synapse + PostgreSQL + Redis via
  Docker Compose; first-run config/key generation into a git-ignored volume;
  secrets supplied from a git-ignored `.env` (see `deploy/compose/README.md`).
- **CI** `.github/workflows/neuron-ci.yml` — ruff + mypy + pytest, path-filtered
  to `neuron/**` so it never touches Synapse's CI.
- **Dev helper** `scripts/dev-setup.sh`; **`.gitignore`** for venvs/secrets/caches.

### Verified locally (in the build environment)
- `pip install -e ".[dev]"` succeeds (Python 3.11).
- `ruff check .` — **all checks passed**.
- `mypy` — **no issues** in the source files.
- `pytest -q` — **8 unit tests pass**; the integration test collects and **skips**
  cleanly (no live Synapse configured here).

### NOT yet verified here (no Docker daemon in the build sandbox)
- `docker compose up` bringing up Synapse, and the integration smoke test against
  it. The compose file is syntactically validated with `docker compose config`,
  but a live boot must be run in an environment with a Docker daemon. **This is
  part of the Phase 0 review gate.**

### Phase 0 acceptance criteria status
- [⏳] `docker compose up` → Synapse reachable, `/_matrix/client/versions` responds
  — *needs a Docker daemon to confirm.*
- [⏳] `neuron_core` lists users via `GET /_synapse/admin/v2/users` against the dev
  Synapse — *unit-tested against a mock; live run needs the Docker daemon.*
- [✅] CI workflow defined; lint + types + unit tests green locally.

### Review gate
Please review the skeleton, stack choices, and dev workflow. When you (or a
Docker-capable environment) confirm the compose bring-up + integration test,
Phase 0 is complete and we proceed to **Phase 1 — neuron-console (read-only)**.

---

## Phase 1 — neuron-console (read-only) — ✅ built, awaiting review gate

**Goal:** a usable web console that *reads* the homeserver.

### Delivered
- **`neuron_core` admin client extended** with the read endpoints the console
  needs: `list_rooms`, `get_room`, `get_room_members`, `get_room_state`,
  `list_event_reports` (plus `RoomListPage` / `EventReportPage` result types).
- **`neuron_console` service** (`src/neuron_console/`): a FastAPI app
  (`create_app()` factory + module-level `app` for uvicorn) with:
  - Operator login via a configured console password (`NEURON_CONSOLE_PASSWORD`)
    and a signed session cookie (Starlette `SessionMiddleware`). The Synapse
    **server-admin token stays server-side** and is never sent to the browser.
  - Server-rendered pages (Jinja2, no client-side build step): **dashboard**
    (server version + user/room counts), **users** (list + search + pagination)
    and **user detail** (profile, 3PIDs, external IDs), **rooms** (list + search
    + pagination) and **room detail** (members + state), **content reports**, a
    friendly **error** page for Synapse failures, plus `/healthz`.
  - Minimal dependency-free CSS (`static/style.css`).
- **Tests** (`tests/neuron_console/test_app.py`): auth gating, login success/
  failure, page rendering, and an explicit assertion that the admin token never
  appears in any response body. The integration smoke test now also exercises
  `list_rooms`.

### Verified locally
- `ruff check .` clean, `mypy` clean (11 source files), `pytest` →
  **15 passed, 3 skipped** (integration skips without a live Synapse).
- Booted the real ASGI app in-process: `/healthz` 200, `/login` renders the
  form, `/` redirects unauthenticated users to `/login`; JSON structured logging
  confirmed.

### Run it
```bash
cd neuron && . .venv/bin/activate
export NEURON_SYNAPSE_BASE_URL=http://localhost:8008
export NEURON_SYNAPSE_ADMIN_TOKEN=<server-admin token>   # see deploy/compose/README.md
export NEURON_CONSOLE_PASSWORD=<a password you choose>
uvicorn neuron_console.app:app --reload --port 8080
# open http://localhost:8080
```

### Phase 1 acceptance criteria status
- [✅] Log in, browse users and rooms, open detail pages — covered by app tests;
  ready for manual click-through against the dev Synapse.
- [✅] No admin token is ever sent to the browser — asserted by a test.
- [⏳] Integration tests against a real dev Synapse — written; run them with the
  dev stack up (needs a Docker daemon, as in Phase 0).

### Review gate
Please click through the console against your dev Synapse (use the run command
above). When it looks right, we proceed to **Phase 2 — console write actions +
MAS/OIDC auth duality**.

---
## Phase 2 — neuron-console (write) + auth-mode handling — ✅ built, awaiting review gate

**Goal:** safe administrative *actions* + production-grade auth handling.

**Scope note:** per the approved decision (classic-auth-first), this phase
implements the full **classic** write path and **designs in** MAS/MSC3861
handling (the console disables the endpoints Synapse turns off under MAS, via the
`NEURON_AUTH_MODE` setting). Full MAS/OIDC *operator login* + routing writes to
the MAS admin API is deferred to a later sub-phase.

### Delivered
- **`neuron_core` admin client — write endpoints:** `upsert_user` (create/modify,
  returns created flag), `deactivate_user` (with erase), `reset_password`,
  `set_shadow_ban`, `create_registration_token` / `delete_registration_token`,
  `send_server_notice`, `set_room_block`, `delete_room` (async → `delete_id`) +
  `get_room_delete_status`, `redact_user_events` (async → `redact_id`) +
  `get_redact_status`. Shared request helpers (`_request`/`_ok_json`) handle
  errors uniformly.
- **Settings:** `synapse_server_name` (to build MXIDs) and `auth_mode`
  (`classic`|`mas`) added to `NeuronCoreSettings`, with `mas_enabled()` and
  `build_user_id()` helpers.
- **Console write actions:** create user, edit profile (+ admin flag in classic
  mode), reset password (classic only), shadow-ban toggle, deactivate (with
  confirm + optional erase), redact-all-messages (async status page), room
  block/unblock, room delete (confirm + async status page), registration tokens
  (list/create/delete), and server notices.
- **Safety:** CSRF protection on every state-changing form (synchroniser-token
  pattern in `security.py`); confirmation pages + `confirm()` prompts for
  destructive actions; async operations poll via auto-refreshing status pages;
  one-shot "flash" success messages.
- **MAS guard:** under `NEURON_AUTH_MODE=mas`, password reset is blocked with a
  clear message and the admin-flag controls are hidden.

### Verified locally
- `ruff` clean, `mypy` clean (12 source files), `pytest` → **20 passed,
  3 skipped**. Tests cover: auth gating, CSRF (invalid token rejected + action
  not performed), create user, deactivate, registration-token create, room
  block + delete flow (status reaches "complete"), the MAS guard (reset-password
  → 409), token-never-leaks, and that **every page template renders**.

### Phase 2 acceptance criteria status
- [✅] Create then deactivate a user end-to-end — tested.
- [✅] Create a registration token — tested.
- [✅] Block/delete a test room with status reaching completion — tested.
- [✅/deferred] MAS handling — the *guard* (disable MAS-disabled endpoints) is
  implemented and tested; full MAS/OIDC operator login is deferred per the
  classic-first decision.

### Review gate
Click through the write actions against the dev Synapse (create a user, create a
token, block/delete a throwaway room). When happy, we proceed to **Phase 3 —
neuron-supervisor (plaintext moderation)**.

---
## Phase 3 — neuron-supervisor (plaintext moderation) — ✅ built, awaiting review gate

**Goal:** a privileged bot that can be promoted into rooms and moderate
*unencrypted* ones.

### Delivered
- **`neuron_core` clients:** added `MatrixClient` (Client-Server API: `whoami`,
  `joined_rooms`, `kick`, `ban`, `redact_event`, get/set power levels) for acting
  *as the bot*; refactored shared HTTP error handling into `_http.py` and a
  `MatrixApiError` base (with `SynapseAdminError` / `MatrixError` subclasses).
  Added `make_room_admin` and `force_join` to the admin client.
- **`neuron_supervisor` package:**
  - `Supervisor` — `ensure_admin(room)` / `ensure_admin_in_all_rooms()` (promote
    the bot to highest power via `make_room_admin`, recording per-room success/
    error), `kick` / `ban` (as the bot), `redact_user` (server-side via Admin API).
  - `SupervisorSettings` (bot user ID, bot token, poll interval).
  - `python -m neuron_supervisor sync|run` — one-shot promotion or a poll loop
    (poll-based new-room detection for this phase).
- **Console "Supervision" tab:** `/supervision` page with "Promote bot into all
  rooms"; on each room page, "Promote bot to admin here" and per-member
  **Kick/Ban** buttons (shown when a bot token is configured). All CSRF-protected.
- **Settings:** `NEURON_SUPERVISOR_BOT_USER_ID` / `NEURON_SUPERVISOR_BOT_TOKEN`
  wired into the console.

### Verified locally
- `ruff` clean, `mypy` clean (18 source files), `pytest` → **33 passed,
  3 skipped**. New tests: `MatrixClient` (kick/redact/errors), `Supervisor`
  (promote-all, per-room error capture, kick-requires-bot, redact via Admin API),
  and console supervision routes (promote-all, per-room promote + kick). The CLI
  (`python -m neuron_supervisor --help`) runs.

### Phase 3 acceptance criteria status
- [✅] Supervisor detects rooms (Admin API list), gains PL100 (`make_room_admin`),
  and can kick + redact — covered by unit tests; ready for a live run against the
  dev Synapse (create a plaintext room, run `sync`, then kick/redact from the UI).
- [✅] All actions triggerable from the console Supervision tab — implemented +
  tested.

### Review gate
Configure a bot account + token, click through the Supervision tab (promote, then
kick a member / redact a user). When happy, we proceed to **Phase 4 —
neuron-auditor (plaintext)**.

---
## Phase 4 — neuron-auditor (plaintext) — ⬜ not started
## Phase 5 — E2EE for auditor & supervisor — ⬜ not started
## Phase 6 — neuron-mediascan (ClamAV) — ⬜ not started
## Phase 7 — neuron-directory (IAM / GroupSync) — ⬜ not started
## Phase 8 — neuron-gateway (federation firewall) — ⬜ not started
## Phase 9 — neuron-scale (HA blueprint) + hardening — ⬜ not started
