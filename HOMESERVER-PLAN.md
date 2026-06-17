# Neuron Homeserver — Clean-Room Plan

> **Status:** plan for approval. **No homeserver code is written yet.** This
> document describes how we build an original, clean-room Matrix homeserver and
> fold it into Neuron so the project becomes a single, self-owned, all-in-one
> stack. It complements `PLAN.md` (the services plan) and `ARCHITECTURE.md`.

## Decisions recorded (from review)

| Decision | Choice |
|---|---|
| License (whole project) | **Apache-2.0** (relicense existing Neuron code + new homeserver) |
| Homeserver language | **Python 3.11+** (one stack with the rest of Neuron) |
| First milestone | **Non-federating, single-server MVP first** (federation is a later epic) |
| During the build | **Keep stock open-source Synapse running**; swap to ours once it reaches parity |

## What we are building

An original Matrix homeserver — call it **`neuron_server`** (product name *Neuron
Server*; adjustable, no Element/ESS trademarks) — that implements the **open
Matrix Client-Server API** so real clients (Element, etc.) and our own Neuron
services can talk to it. It will live alongside the existing services so that,
when ready, Neuron is one self-contained system: **our homeserver + our services,
one codebase, one license (Apache-2.0).**

Because the Neuron services already speak the standard Matrix/Admin APIs, the
homeserver simply *replaces the thing they point at* — no rework of the console,
bots, or `neuron_core` (provided we expose a Synapse-compatible Admin API; see
Phase HS-6).

## The non-negotiable clean-room rule

To keep the result original and Apache-licensable, the homeserver is implemented
**only from the open Matrix specification and open MSCs**:

- **Allowed sources:** the Matrix spec (`spec.matrix.org` / `matrix-org/matrix-spec`),
  the published `matrix-spec-proposals` (MSCs), the open event-schemas, and the
  **Complement** conformance test-suite (Apache-2.0) used as a *black-box* checker.
- **Forbidden:** reading, copying, translating, or "porting" the source of
  **Synapse, Dendrite, Conduit/conduwuit, or any existing homeserver.** The spec
  is explicitly designed for independent implementations — that is our only
  reference. Copying any AGPL homeserver would make our code a derivative and
  defeat both the relicense and the clean-room goal.
- **Provenance discipline:** each module cites the spec section(s) it implements
  (as we did in `docs/feature-analysis.md`). We never look at another server's
  code to "see how they did it."

Note: running stock open-source Synapse as a black box during development (the
"keep Synapse" decision) is **not** a clean-room issue — we use it over its public
APIs and as a behavioral reference *via the spec/Complement*, never by reading its
source.

## Architecture (Python, single-server first)

```
                 Matrix clients (Element, etc.)  +  Neuron services
                                  │  Client-Server API (/_matrix/client/...)
                                  ▼
   ┌───────────────────────────── neuron_server ─────────────────────────────┐
   │  HTTP/ASGI layer (FastAPI/Starlette)  — endpoint routing, auth, JSON      │
   │  ──────────────────────────────────────────────────────────────────────  │
   │  Domain services:                                                         │
   │    auth/devices · rooms · events+state · auth-rules (per room version)    │
   │    sync · media · account-data/profile · E2EE key storage (relay only)    │
   │    admin API (Synapse-compatible surface)                                 │
   │  ──────────────────────────────────────────────────────────────────────  │
   │  Storage layer (async): SQLite (dev) / PostgreSQL (prod)                  │
   └──────────────────────────────────────────────────────────────────────────┘
   (Federation / Server-Server API is a later epic — see Phase HS-7.)
```

Principles: clean separation (API → domain services → storage); room-version-aware
event handling; the server **never decrypts** E2EE — it only stores/relays device
keys, one-time keys, key backups, and to-device messages (crypto stays client-side,
exactly as our `neuron_crypto` already does on the bot side).

Likely libraries (all permissive, Apache-compatible): FastAPI/Starlette, `asyncpg`
or SQLAlchemy-core, `signedjson`/`unpaddedbase64`/`canonicaljson` (Matrix
event signing & canonical JSON — these are the standard MIT/Apache Matrix
primitives, not homeserver code), `cryptography`/PyNaCl (Ed25519).

## Phased plan (each phase: independently testable + a review gate)

Smallest-working-server-first. We validate with real clients and the open
**Complement** suite, never by copying another server.

- **HS-0 — Foundation & spec harness.** `neuron_server` skeleton; config; async
  storage layer (SQLite dev / Postgres) + migrations; `GET /_matrix/client/versions`
  and `/.well-known/matrix/client`. **Done when:** a client sees a valid Matrix
  server and DB migrations run. *(Effort: M)*
- **HS-1 — Identity & auth.** Register (password/dummy flow), login, logout,
  whoami, access tokens, device management. **Done when:** you can register + log
  in via our server, and `neuron_core`'s client can authenticate against it. *(M)*
- **HS-2 — Rooms, events & auth rules (the core).** `createRoom`; core state
  events (`m.room.create/member/power_levels/join_rules/...`); send message events;
  `/state`, `/messages` (pagination), redactions; event IDs/hashing/signing and
  **per-room-version auth rules**. **Done when:** rooms enforce the spec's auth
  rules; messages send/read correctly. *(L–XL — auth rules are substantial)*
- **HS-3 — Sync.** `GET /sync` (initial + incremental: timeline, state,
  account_data, to_device, device_lists, since-tokens). **Done when:** a real
  client (and our auditor) syncs and sees live messages. *(L)*
- **HS-4 — Media repository.** Authenticated upload/download/thumbnail/config.
  **Done when:** media round-trips; works behind `neuron-mediascan`. *(M)*
- **HS-5 — E2EE server support.** Device keys upload/query/claim, one-time keys,
  key backup (`/room_keys`), `sendToDevice`, device-list tracking — store/relay
  only. **Done when:** two clients (or our auditor + a client) do E2EE end-to-end
  against our server; our automatic key receipt works. *(L)*
- **HS-6 — Remaining CS API + Admin API.** account data, profiles, filters,
  capabilities, typing/receipts, presence (can stub), push rules (can stub); plus
  a **Synapse-compatible `/_synapse/admin/...` surface** so the Neuron console &
  bots work unchanged. **Done when:** the whole existing Neuron stack runs against
  `neuron_server` instead of Synapse. *(L)*
- **HS-7 — Federation (separate epic, later).** Server keys + `/.well-known/server`
  + `/_matrix/key/v2`; transactions (PDUs/EDUs); make/send join, invite, knock;
  backfill, `event_auth`, `state_ids`; **state resolution v2**; room-version auth
  rules across versions; server ACLs; retry/backoff. **This is the hard, research-
  grade part.** Planned and gated on its own. *(XL — months; state resolution is
  the crux)*
- **HS-8 — Parity, conformance & cut-over.** Pass a growing subset of Complement;
  performance/caching; optional workers (reuse `neuron-scale` ideas); then swap the
  dev stack + Neuron services from stock Synapse to `neuron_server`. *(L, ongoing)*

Reality check (honest): **HS-0 → HS-6 gives a usable, non-federating homeserver**
your clients and Neuron services can run on — achievable incrementally. **HS-7
(federation) is a major undertaking** and is where most homeserver projects spend
years; we treat it as its own program of work, not a checkbox.

## Relicensing to Apache-2.0 (first actionable step)

Done once, up front, before/at the start of homeserver work:

1. Add a top-level **`LICENSE`** containing the Apache-2.0 text, and a **`NOTICE`**
   (Apache convention) stating copyright and that Neuron is an **independent,
   clean-room implementation** not derived from any AGPL homeserver.
2. Change `neuron/pyproject.toml` `license` to `Apache-2.0` (+ classifier) and
   update the README license section.
3. Add short SPDX headers (`# SPDX-License-Identifier: Apache-2.0`) to source files
   (a small script can do this consistently).
4. **Dependency audit (already favourable):** all current deps are permissive
   (httpx=BSD; pydantic/fastapi/jinja2=MIT/BSD; boto3=Apache; libolm/python-olm=
   Apache; signedjson/canonicaljson/unpaddedbase64=Apache/MIT). No copyleft
   contamination; nothing blocks Apache-2.0. Once `neuron_server` replaces Synapse,
   the project has **no AGPL dependency at all**.
5. Record the clean-room provenance (sources = spec/MSCs/Complement only) in
   `NOTICE`/this plan so the licensing is defensible.

This is safe because our existing Neuron code is independent of Synapse (separate
process, public APIs) — so relicensing it is purely our choice.

## How it integrates with the existing Neuron services

- `neuron_server` is a new package under `neuron/src/neuron_server/`, run as its own
  process/container (its own image), exactly like Synapse is today.
- The dev `docker-compose` keeps a `synapse` service **and** gains a `neuron-server`
  service behind a profile, so you can run the Neuron services against *either*
  while we build toward parity.
- Because we expose a Synapse-compatible Client-Server + Admin API, `neuron_core`,
  the console, the bots, and the scanner need **no changes** to point at our server.

## Hardest risks (called out honestly)

- **State resolution v2 + per-version auth rules** (HS-7/part of HS-2): subtle,
  the classic source of homeserver bugs. Mitigation: implement strictly from the
  spec, test against Complement, keep room versions scoped.
- **Federation interop** (HS-7): real-world federation is unforgiving; many edge
  cases. Mitigation: treat as a dedicated epic; Complement first, then careful
  interop testing with a *separate* server instance.
- **Performance** of a Python homeserver at scale: fine for self-host/small-medium;
  large scale needs the worker/sharding work (HS-8).
- **Scope/time:** "complete + federating + spec-compliant" is long. Mitigation: the
  phased gates above always leave you with working software, and Synapse keeps the
  lights on until cut-over.

## Open questions for you

1. **Homeserver name:** `neuron_server` / *Neuron Server* OK, or prefer another
   (still trademark-safe)?
2. **Admin API:** expose a **Synapse-compatible** `/_synapse/admin/...` surface
   (so existing Neuron tooling works unchanged — recommended), or define our own
   `/_neuron/admin/...` and add a small switch to `neuron_core`?
3. **Scope confirmation:** are you happy to *stop at HS-6 (non-federating)* for the
   foreseeable future and treat HS-7 (federation) as a separate later decision?
4. **Relicense now?** Shall I do the Apache-2.0 relicense (step above) as the very
   first commit, before any homeserver code?

---

*End of plan. On approval I'll start with the Apache-2.0 relicense, then HS-0
(foundation), committing per phase with tests and stopping at each review gate —
same rhythm as the services build.*
