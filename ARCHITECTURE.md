# Neuron — Architecture (Phase 2 design)

> **Status:** proposal for your approval. **No code, manifests, or configs are written
> yet** — this document and its siblings (`FEATURE-MATRIX.md`, `PLAN.md`,
> `OPEN-QUESTIONS.md`) describe *what we intend to build* and *why*. The behavioral
> research that justifies every design choice is in `docs/feature-analysis.md`.

## 1. What we are building, in one paragraph

This repository is **Synapse** (the open-source Matrix homeserver, v1.155.0rc1).
**Neuron** is a suite of *independent services that sit around a stock, unmodified
Synapse* and talk to it only over **public Matrix APIs** (Client-Server, Server-Server,
Application Service) and the **open Synapse Admin API**. Together they reproduce the
*documented functionality* of the features that distinguish Element Server Suite (ESS)
Pro from ESS Community — a federation firewall, directory/identity sync, an audit bot, a
supervision bot, an admin console, a media scanner, and a high-availability deployment
recipe. We do **not** fork or patch Synapse's internals (with one possible, clearly
flagged exception discussed in §7), and we do **not** copy Element's proprietary code.

Why this shape? Because the feature analysis showed that almost every ESS Pro feature is,
underneath the product branding, *a service speaking standard Matrix/Synapse APIs*. Stock
Synapse already exposes everything we need. Keeping our work as separate services means:
(a) clean-room separation from both Synapse's AGPL code and Element's proprietary code is
obvious and auditable; (b) we never have to rebase against upstream Synapse; (c) each
service is small enough for a beginner to understand and test in isolation.

## 2. Original component names (no Element/ESS trademarks)

The umbrella project is **Neuron**. Each service has a plain, descriptive name. (Naming
is open for your input — see `OPEN-QUESTIONS.md` Q1.)

| # | Feature (Element's name) | Our component | One-line role |
|---|--------------------------|---------------|---------------|
| 1 | Federation firewall (*Secure Border Gateway*) | **neuron-gateway** | Edge reverse-proxy enforcing allow/deny + header rules on `/_matrix` traffic |
| 2 | Advanced IAM (*GroupSync*) | **neuron-directory** | Syncs LDAP/SCIM/AD → Matrix users, spaces, memberships, power levels |
| 3 | Audit logging (*AuditBot*) | **neuron-auditor** | Bot/appservice that streams room events as JSON to filesystem/S3 |
| 4 | Central supervision (*AdminBot*) | **neuron-supervisor** | Privileged bot promoted into every room for moderation |
| 5 | Admin console (*Element Admin*) | **neuron-console** | Web UI + backend over the Synapse Admin API |
| 6 | Media content scanner | **neuron-mediascan** | Proxy that AV-scans (ClamAV) media before delivery |
| 7 | Scalability / HA (*Synapse Pro outcomes*) | **neuron-scale** | Deployment blueprint: workers + Redis + Postgres + reverse proxy |

Shared code lives in one library:

- **neuron-core** — a small Python package with: a typed **Synapse Admin API client**, a
  thin **Client-Server API client**, config loading, structured logging, and shared auth
  helpers. Every service depends on it so we write the "call Synapse" logic once.

## 3. Where the code will live (repository layout)

To keep Neuron clearly separate from Synapse's own tree (`synapse/`, `rust/`, `tests/`,
`docs/`, …), **all new code goes under a single top-level `neuron/` directory.** Planning
docs live at the repo root (this file and its siblings); the Phase 1 analysis stays at
`docs/feature-analysis.md` as you requested.

```
Neuron/                         # (this repo = Synapse + our additions)
├── ARCHITECTURE.md             # ← you are here
├── FEATURE-MATRIX.md
├── PLAN.md
├── OPEN-QUESTIONS.md
├── docs/feature-analysis.md    # Phase 1 research
├── synapse/ rust/ tests/ ...   # UNTOUCHED upstream Synapse
└── neuron/                     # ALL Neuron code (added in Phase 3)
    ├── README.md
    ├── libs/
    │   └── neuron_core/        # shared library (admin client, config, logging)
    ├── services/
    │   ├── gateway/            # Feature 1
    │   ├── directory/          # Feature 2
    │   ├── auditor/            # Feature 3
    │   ├── supervisor/         # Feature 4
    │   ├── console/            # Feature 5  (backend/ + web/)
    │   └── mediascan/          # Feature 6
    ├── deploy/                 # Feature 7 + local dev
    │   ├── compose/            # docker-compose dev stack (Synapse, Postgres, Redis, LDAP, ClamAV, services)
    │   ├── synapse-workers/    # worker configs + reverse-proxy routing
    │   └── k8s/                # optional Kubernetes manifests
    └── tests/                  # integration tests against a local dev Synapse
```

We will **not** touch Synapse's `pyproject.toml`/`poetry.lock`. Neuron gets its **own**
Python project file under `neuron/` so our dependencies never collide with Synapse's.

## 4. Technology choices (and why — written for a beginner)

The guiding principle is **one language, well-supported libraries, minimal moving parts**,
so you can read and debug everything. We standardize on **Python 3.11+**.

| Concern | Choice | Why (plain terms) |
|---|---|---|
| Language | **Python 3.11+** | Same language as Synapse, huge Matrix ecosystem, easy to read. One language across all services means one set of skills to learn. |
| Web services / APIs / proxy | **FastAPI** (on Starlette/Uvicorn) + **httpx** | FastAPI is the most beginner-friendly modern Python web framework: automatic docs, type hints, async. `httpx` is a clean HTTP client we use to call Synapse and to proxy requests. |
| Matrix bots (auditor, supervisor) | **matrix-nio** (with the E2EE/`olm` extra) | A well-documented Matrix client SDK that supports **end-to-end encryption** — essential for the audit/supervision bots. (`mautrix-python` is the main alternative; see OPEN-QUESTIONS Q6.) |
| Synapse Admin API access | our **neuron-core** client (httpx under the hood) | We wrap the documented admin endpoints once, with types and tests, so each service just calls Python methods. |
| Admin console UI | **FastAPI + Jinja2 templates + HTMX** (recommended) | Lets us build an interactive web UI **without a separate JavaScript build toolchain or React knowledge** — far gentler for a beginner. React/TypeScript is the heavier industry-standard alternative (OPEN-QUESTIONS Q3). |
| LDAP / directory | **ldap3** | The standard pure-Python LDAP library; works with OpenLDAP, AD, Samba. |
| SCIM intake | a small **FastAPI** endpoint | SCIM is "just" a REST/JSON standard; we expose the endpoints an IdP pushes to. |
| Media scanning | **ClamAV** via the **clamd** protocol | ClamAV is the documented, free AV engine; we talk to its daemon over a socket. |
| Local dev + packaging | **Docker / docker-compose**, **distroless** runtime images | One command brings up Synapse + Postgres + Redis + our services for testing. Distroless images match the ESS Pro packaging outcome and are smaller/safer. |
| Tests | **pytest** + a disposable **dev Synapse** in compose | Each service is tested against a real (throwaway) homeserver, not mocks where it matters. |
| Config | **YAML files + environment variables**, validated with **Pydantic** | Human-readable config; secrets come from the environment, never the file. Pydantic catches mistakes early with clear error messages. |

## 5. How each service talks to Synapse (data flow & auth)

All services treat Synapse as a black box reached over HTTPS. Three integration styles
appear, matched to each feature's needs (full endpoint lists are in
`docs/feature-analysis.md`):

```
                       ┌─────────────────────────────────────────────┐
   remote homeservers  │                  neuron-gateway              │  (edge firewall)
   ───────────────────▶│  inspect X-Matrix origin/dest + client hdrs  │
   clients ───────────▶│  allow/deny → forward or 403 M_FORBIDDEN     │
                       └───────────────┬─────────────────────────────┘
                                       │  /_matrix/... (unmodified URIs)
                                       ▼
                              ┌──────────────────┐         ┌───────────────┐
                              │   STOCK SYNAPSE   │◀───────▶│  PostgreSQL   │
                              │  (homeserver)     │         └───────────────┘
                              │  + Admin API      │◀───────▶│  Redis (HA)   │
                              └───┬───────┬───────┘         └───────────────┘
        Admin API (server-admin token)   │ CS API (bot login) / AS API (appservice)
        ┌──────────────┼───────────────┐ │ ┌───────────────┬───────────────┐
        ▼              ▼               ▼ ▼ ▼               ▼               ▼
  neuron-console  neuron-directory  neuron-supervisor  neuron-auditor  neuron-mediascan
  (Admin API)     (Admin+CS API)    (Admin+CS API)     (CS/AS+E2EE)    (media proxy)
                                                                         │
                                                                    ClamAV daemon
```

Integration styles:

- **Admin-API client (server-admin token).** Used by **neuron-console**,
  **neuron-directory**, and partly **neuron-supervisor**. They hold a Synapse
  *server-admin access token* and call `/_synapse/admin/...`. The token is a secret,
  injected at runtime.
- **Bot account (Client-Server API login).** Used by **neuron-auditor** and
  **neuron-supervisor** when they must *act as a member* of rooms (read timelines, send
  redactions, hold power level 100, participate in E2EE). Each bot logs in with its own
  credentials and keeps a persistent device + crypto store.
- **Application Service (AS API).** An optional, higher-scale ingestion path for
  **neuron-auditor** (receive every event via `PUT /_matrix/app/v1/transactions/{txnId}`
  over broad namespaces) and the identity for **neuron-supervisor**. Requires an
  appservice registration file (with `as_token`/`hs_token`) installed on Synapse.
- **Transparent proxy.** **neuron-gateway** (in front of Synapse) and **neuron-mediascan**
  (in front of the media repo) forward HTTP requests, applying policy. The gateway must
  **not** rewrite/canonicalize URIs (it would break federation X-Matrix signatures).

## 6. Per-service design summaries

Each service is one container, independently deployable and testable.

### 6.1 neuron-gateway (Feature 1 — federation firewall)
- **Shape:** an ASGI reverse proxy (FastAPI/Starlette + httpx streaming) placed at the
  edge, in front of Synapse's client (443) and federation (8448) ports.
- **Logic:** for **federation** requests, parse the `Authorization: X-Matrix` header,
  confirm `origin` is on the allow list and `destination` is us; optionally verify the
  Ed25519 signature using the origin's key (`/_matrix/key/v2/server`). For **client**
  requests, enforce required-header rules. On failure return `403` with a standard
  `{"errcode":"M_FORBIDDEN",...}` body. Otherwise stream the request through unchanged.
- **Why a Python proxy first:** it is the most *readable* way to learn the policy logic.
  For production HA we document a path to nginx/HAProxy or an Envoy `ext_authz` sidecar
  (the gateway becomes the authorization service; the heavy proxying moves to Envoy).
  See OPEN-QUESTIONS Q8.
- **Key correctness rule:** preserve the raw request URI and body exactly (no `%xx`
  decoding) so X-Matrix signatures still verify.

### 6.2 neuron-directory (Feature 2 — identity/directory sync)
- **Shape:** a long-running reconciliation service with two halves mirroring Element's
  *Bridge → Provisioner* split: a **reader** (LDAP/SCIM/Graph → a normalized "desired
  state" model) and a **provisioner** (apply desired state to Synapse).
- **Logic:** on a timer (and on SCIM push), read the directory; compute the desired set of
  users, spaces (`m.space`), memberships, and power levels; diff against Synapse's actual
  state; apply changes via the Admin API (`PUT /_synapse/admin/v2/users/{id}`, force-join)
  and CS API (createRoom, `m.space.child`, `m.room.power_levels`). Run **room cleanup** as
  an idempotent post-pass. Implement the **lock → deactivate(grace) → erase** lifecycle.
- **Scoping:** only manage local users; reconcile by immutable external id.

### 6.3 neuron-auditor (Feature 3 — audit logging)
- **Shape:** a Matrix client bot (matrix-nio + E2EE) with a persistent device and crypto
  store, plus pluggable **output sinks** (filesystem JSONL, S3).
- **Logic:** auto-join/invite into rooms; `/sync` for live events; decrypt where keys are
  available; write each event as JSON to the sink. For history, optionally import room
  keys from server-side key backup via SSSS.
- **E2EE reality (documented honestly):** decryption is *forward-only* — pre-join messages
  can't be read without key sharing/backup. The bot records "could not decrypt (no
  session)" rather than dropping events. The output store is plaintext and must be access
  controlled. (Full analysis: `docs/feature-analysis.md` Feature 3.)

### 6.4 neuron-supervisor (Feature 4 — central supervision)
- **Shape:** a privileged bot (its own account/appservice) + a small control surface used
  by neuron-console.
- **Logic:** detect new rooms (appservice events or polling `GET /_synapse/admin/v1/rooms`),
  then `POST .../make_room_admin` and/or `POST /_synapse/admin/v1/join/...` to get the bot
  into the room at the highest available power. Once in, moderate via CS API
  (power-levels, kick/ban, redact) and Admin API (delete/block room, purge history,
  quarantine media). E2EE moderation reuses neuron-auditor's crypto approach.

### 6.5 neuron-console (Feature 5 — admin console)
- **Shape:** **backend** (FastAPI) that wraps the Synapse Admin API + handles operator
  auth; **web** UI (Jinja2 + HTMX) for users, rooms, media, reports, registration tokens,
  server notices, and a (pluggable) supervision tab.
- **Auth duality:** support both a static Synapse server-admin token (simple/legacy) and a
  MAS-issued admin-scoped OIDC session (when MSC3861 delegated auth is on). Under MSC3861,
  route the endpoints Synapse disables (password/admin-flag/login-as-user) to MAS's admin
  API. See OPEN-QUESTIONS Q4.
- **Safety:** confirmations + async status polling for destructive actions (room delete,
  purge, redact).

### 6.6 neuron-mediascan (Feature 6 — media scanner)
- **Shape:** a FastAPI proxy in front of Synapse's media repo, talking to a **ClamAV**
  daemon; an in-process scan-result cache.
- **Logic:** intercept downloads (`/_matrix/client/v1/media/...` and legacy
  `/_matrix/media/v3/download/...`), fetch bytes from Synapse, scan via ClamAV; serve on
  clean, return a `403`/quarantine error on a hit. Support encrypted-media scanning
  (decrypt server-side from posted keys, scan, serve) and, optionally, upload-time gating
  via a Synapse spam-checker module callback.
- **Build choice:** clean reimplementation for learning, *or* deploy the open-source
  `matrix-content-scanner-python` as-is. See OPEN-QUESTIONS Q10.

### 6.7 neuron-scale (Feature 7 — HA/scaling blueprint)
- **Shape:** *not a service* — a documented deployment kit using **stock** Synapse:
  worker process configs (`generic_worker`, media worker, federation senders, stream
  writers), **Redis** replication, **external PostgreSQL**, a reverse proxy routing
  endpoint patterns to workers, health checks, and autoscaling notes.
- **Honest limit:** stock Synapse needs restarts to re-shard and lacks Pro's Rust
  multi-core/shared-cache efficiency. We match the *outcome* (no app-process single point
  of failure, horizontal scale, failover), not the proprietary internals.

## 7. The one place server internals might be touched (flagged)

Everything above is external. The **only** features that *could* benefit from a Synapse
**module** (a documented, supported extension point — not a fork) are:
- **neuron-mediascan**'s optional *upload-time* gate, via the `check_media_file_for_spam`
  spam-checker callback, and
- **neuron-directory**'s optional invite/permission policy, via spam-checker callbacks.

These use Synapse's **public module API** (`docs/modules/…`), load as plugins, and do
**not** modify Synapse source. We will prefer the external proxy/service approach and use
a module only where the documentation shows it is the intended hook. This will be called
out explicitly wherever used.

## 8. Deployment model

- **Local development:** a `docker-compose` stack under `neuron/deploy/compose/` brings up
  stock Synapse + PostgreSQL + Redis + a test OpenLDAP + ClamAV + each Neuron service.
  This is the "local dev Synapse to validate against" required for Phase 3 testing.
- **Single-host production:** the same compose stack, hardened, behind neuron-gateway.
- **Scaled/HA production:** `neuron-scale` worker layout, optionally on Kubernetes
  (`neuron/deploy/k8s/`). Target deployment is an open question (OPEN-QUESTIONS Q7).
- **Images:** each service ships as a small **distroless** container.

## 9. Authentication & secrets strategy

- **No secrets in the repo, ever.** We commit `*.example` files with placeholders and a
  documented strategy; real values come from the environment or mounted files.
- **Service → Synapse:** server-admin access tokens, bot passwords/tokens, and appservice
  `as_token`/`hs_token` are injected at runtime (env vars or mounted secret files).
- **Operator → console:** session cookies over HTTPS; CSRF protection; OIDC (MAS) or a
  configured admin token. The console never exposes the raw admin token to the browser.
- **High-value secrets** (audit/supervisor E2EE recovery passphrase, S3 credentials,
  ClamAV none) are documented as top-tier and recommended to live in Kubernetes Secrets or
  a vault. Local dev uses a git-ignored `.env`.
- **Crypto stores** (bot device + Megolm keys) persist on a mounted volume, mirroring
  ESS's PVC pattern; they are sensitive and access-controlled.

## 10. Clean-room guardrails (operational)

- Sources limited to public docs, the open Matrix spec, and OSS libraries — recorded with
  citations in `docs/feature-analysis.md`.
- No Element/ESS proprietary source or images are read, copied, or decompiled.
- No Element/ESS trademarks in names, packages, or UI.
- Synapse's own tree is left unmodified; Neuron is additive and isolated under `neuron/`.

---

*Next: `FEATURE-MATRIX.md` (feature → approach → APIs → libs → effort → risks),
`PLAN.md` (phased build with acceptance criteria and review gates), and
`OPEN-QUESTIONS.md` (decisions we need from you).*
