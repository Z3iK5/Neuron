# Neuron — Build Plan (Phase 2 design)

> A phased, **smallest-working-slice-first** plan. Each phase is **independently
> testable**, has explicit **acceptance criteria**, and ends at a **review gate** where we
> stop, you check the work, and you approve moving on. Nothing here is built until you
> approve this plan (and `ARCHITECTURE.md` / `FEATURE-MATRIX.md`).

## Principles

1. **Lowest-risk, highest-learning first.** We start with a foundation phase, then the
   **admin console** (read-only) — it touches only the well-documented Synapse Admin API,
   has no E2EE, and gives you something visible and useful immediately.
2. **Defer E2EE until we have momentum.** The audit/supervision bots' encrypted-room
   support is the hardest part; we reach it after several easier wins, and even then build
   **plaintext-first**, adding E2EE as a discrete, separately-reviewed step.
3. **Every phase validates against a real, local, throwaway Synapse**, not mocks.
4. **Commit per phase** with clear messages; maintain `PROGRESS.md`; **stop at each gate.**
5. **Secrets never enter the repo** — placeholders + documented injection from the start.

## Ordering rationale (why this sequence)

```
P0 Foundation ─┬─▶ P1 Console (read-only)  ◀── lowest risk, immediately useful
               │      │
               │      ▼
               │   P2 Console (write) + neuron-core admin client matured
               │      │
               ├─▶ P3 Supervisor (plaintext moderation)   ── reuses admin client
               │
               ├─▶ P4 Auditor (plaintext)  ──▶ P5 Auditor + Supervisor E2EE  ◀── hardest
               │
               ├─▶ P6 Mediascan (ClamAV)
               │
               ├─▶ P7 Directory sync (IAM)  ◀── largest; benefits from mature admin client
               │
               └─▶ P8 Gateway (firewall)  ─▶ P9 Scale/HA blueprint
```

The console comes first (you asked for either console or audit bot as the first slice; the
console is strictly lower risk — no crypto, no bot accounts). The directory sync (largest)
comes after the admin client is battle-tested by the console and supervisor. E2EE is
isolated into its own phase so a setback there can't block everything else.

---

## Phase 0 — Foundation & local dev environment
**Goal:** a working monorepo skeleton and a one-command local Synapse to test against.
- Create `neuron/` layout (libs, services, deploy, tests) and Neuron's own Python project
  (separate from Synapse's).
- `neuron-core` v0: typed **Synapse Admin API client** (a few read endpoints) + config
  loading (Pydantic) + structured logging.
- `docker-compose` dev stack: stock Synapse + PostgreSQL (+ Redis stub), with a registered
  admin user and a generated admin token (documented, git-ignored).
- CI: lint (ruff) + type-check (mypy) + run unit tests; a session-start hook so web
  sessions can run tests/linters.
- `PROGRESS.md` initialized.
**Acceptance criteria:**
- `docker compose up` brings up Synapse reachable on its client port; `/_matrix/client/versions` responds.
- A `neuron-core` test logs in/uses the admin token and lists users via
  `GET /_synapse/admin/v2/users` against the dev Synapse.
- CI is green on a trivial PR.
**Review gate:** you confirm the skeleton, stack choices, and dev workflow before we build features.

## Phase 1 — neuron-console (read-only)
**Goal:** a usable web console that *reads* the homeserver.
- Backend (FastAPI) endpoints wrapping admin reads: list/search users, view a user, list
  rooms, view a room (members/state), event reports, statistics, media list.
- Web UI (Jinja2 + HTMX): user list + detail, room list + detail, dashboards. Operator
  login via a configured admin token (OIDC/MAS deferred to Phase 2).
- Pagination handled correctly (forward-only users; both-way rooms).
**Acceptance criteria:**
- Log in, browse users and rooms created in the dev Synapse, open detail pages.
- No admin token is ever sent to the browser (verified).
- Integration tests hit a real dev Synapse and assert listed users/rooms.
**Review gate:** you click through the UI and approve before we add write/destructive ops.

## Phase 2 — neuron-console (write) + auth duality
**Goal:** safe administrative *actions* + production-grade auth.
- Write actions: create/modify user, deactivate (with confirm), reset password (non-MAS),
  shadow-ban, registration tokens, send server notice, room block/delete (async + status
  polling), redact (async + status).
- **Auth duality:** add MAS/OIDC operator login; detect MSC3861 and route disabled
  endpoints to MAS's admin API; keep the static-token path for non-MAS deployments.
- Destructive-action UX: confirmations, irreversible-warning, async progress.
**Acceptance criteria:**
- Create then deactivate a user end-to-end; create a registration token; block/delete a
  test room with status reaching completion.
- With MAS enabled in a dev profile, operator OIDC login works and disabled-endpoint
  routing is exercised by a test.
**Review gate:** you approve the console as feature-complete enough to anchor the suite.

## Phase 3 — neuron-supervisor (plaintext moderation)
**Goal:** a privileged bot that can be promoted into rooms and moderate *unencrypted* ones.
- New-room detection (poll `GET /_synapse/admin/v1/rooms`; appservice path optional).
- `make_room_admin` / admin force-join to obtain highest available power.
- Moderation actions (kick/ban, set power levels, redact, delete/block room, purge,
  quarantine media), surfaced in the console's supervision tab.
**Acceptance criteria:**
- Create a plaintext room without the bot; the supervisor detects it, gains PL100, and
  performs a kick + a redact, verified via the API.
- All actions are triggerable from the console supervision tab.
**Review gate:** approve before introducing E2EE.

## Phase 4 — neuron-auditor (plaintext)
**Goal:** stream events from unencrypted rooms to durable outputs.
- matrix-nio bot with persistent device/store; auto-join; `/sync` loop.
- Output sinks: filesystem JSONL and S3 (S3 via a MinIO container in dev).
- Event schema (stable JSON), backfill via `/rooms/{id}/messages`, retry/dedup.
**Acceptance criteria:**
- Messages sent in a plaintext room appear as JSON lines in the filesystem sink and in
  MinIO, with no gaps across a bot restart.
**Review gate:** approve the audit data model + outputs before tackling E2EE.

## Phase 5 — E2EE for auditor & supervisor (the hard phase, isolated)
**Goal:** read/moderate **encrypted** rooms, with honest limits.
- Bot device sets up **cross-signing**; persistent crypto store; receive live Megolm keys.
- Optional **historical key import** from server-side key backup via SSSS (recovery
  passphrase as a top-tier secret).
- Decryption-failure handling: record "could not decrypt (no session)" with the encrypted
  envelope, never silently drop.
**Acceptance criteria:**
- In an encrypted room the bot joined *before* messages were sent, those messages are
  decrypted and audited; pre-join messages are clearly marked undecryptable.
- Supervisor can read/redact a flagged message in an encrypted room.
- Docs state the forward-only limitation and the plaintext-store warning prominently.
**Review gate:** explicit approval — this is where protocol limits become real; we confirm
expectations match reality.

## Phase 6 — neuron-mediascan (ClamAV)
**Goal:** AV-scan media before delivery.
- FastAPI proxy + ClamAV (clamd) container in dev; scan-result cache.
- Intercept downloads; serve clean, block infected (EICAR test file as the canary).
- Encrypted-media scan path; optional upload-time spam-checker module variant.
**Acceptance criteria:**
- Uploading/downloading a clean file succeeds through the proxy; an EICAR test file is
  blocked with a clear error; results are cached on repeat.
**Review gate:** approve scanner behavior + the module-vs-proxy choice.

## Phase 7 — neuron-directory (IAM / GroupSync)
**Goal:** sync a directory to Matrix users/spaces/permissions with a safe lifecycle.
- Test OpenLDAP/Samba in dev. Reader → desired-state model. Provisioner applies users,
  spaces (`m.space.child`), memberships, power levels (0/50). Room cleanup post-pass.
  Lifecycle: lock → deactivate(grace) → erase. SCIM intake endpoint.
**Acceptance criteria:**
- Add a user/group in test LDAP → user provisioned, placed in the right space at the right
  power level. Remove from LDAP → locked, then (simulated grace elapse) erased.
- Re-running a sync makes **no changes** (idempotent); space trees have no loops.
**Review gate:** approve the destructive lifecycle behavior on test data before any real
directory is connected.

## Phase 8 — neuron-gateway (federation firewall)
**Goal:** enforce allow/deny + client-header rules at the edge.
- ASGI reverse proxy; X-Matrix parsing; allow/deny on `origin`; optional Ed25519 verify;
  required client headers; `403 M_FORBIDDEN`. URIs preserved exactly.
- Optional `m.room.server_acl` management helper.
**Acceptance criteria:**
- With a closed allow list, a federation request from a non-allowed origin is `403`; an
  allowed origin passes and federation still works (two dev homeservers).
- A client lacking a required header is `403`; X-Matrix signatures still verify through the
  proxy (no URI canonicalization).
**Review gate:** approve before documenting the production (nginx/Envoy) path.

## Phase 9 — neuron-scale (HA blueprint) + hardening
**Goal:** document & validate a stock-Synapse scaled deployment; finalize distroless images.
- Worker layout (generic workers, media worker, federation senders, stream writers),
  Redis, external Postgres, reverse-proxy routing, health checks, autoscaling notes.
- A compose "workers" profile that actually runs a multi-process Synapse for validation.
- Honest limits documented (restart-to-reshard, single-writer streams, Redis SPOF).
**Acceptance criteria:**
- The workers profile starts; `/sync` is served by a worker; a federation send goes via a
  federation-sender worker; `/health` is green on each process.
- Load smoke-test shows requests fanning out across generic workers.
**Review gate:** final review of the whole suite.

---

## Testing strategy (all phases)
- **Unit tests** for pure logic (config parsing, X-Matrix parsing, diffing, event mapping).
- **Integration tests** against the disposable dev Synapse in compose (real API calls).
- **Canaries:** EICAR for mediascan; a known plaintext + a known encrypted room for the bots.
- **CI** runs lint + types + unit tests on every change; integration tests on a schedule or
  on demand (they need the compose stack).

## Definition of done (per phase)
Acceptance criteria met · tests passing in CI · secrets externalized · `PROGRESS.md`
updated · committed with a clear message · **stopped at the review gate**.

## What we are explicitly NOT doing
- Not forking or patching Synapse internals (modules via the public API only, where noted).
- Not reproducing Synapse Pro's proprietary Rust/shared-cache internals — we match outcomes.
- Not using Element/ESS trademarks, code, or images.
