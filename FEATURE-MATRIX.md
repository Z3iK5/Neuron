# Neuron — Feature Matrix (Phase 2 design)

> Maps each ESS Pro feature to **our implementation approach**, the **open Matrix/Synapse
> APIs** we build against, the **open-source libraries** we use, a **rough effort**
> estimate, and the **hardest risks** (E2EE and Synapse-internals limits called out
> honestly). Behavioral evidence and exact endpoint lists: `docs/feature-analysis.md`.
> Architecture: `ARCHITECTURE.md`.

Effort is a rough t-shirt size for *a beginner working with guidance*:
**S** ≈ a few focused days · **M** ≈ 1–2 weeks · **L** ≈ 3–4 weeks · **XL** ≈ 5+ weeks.

---

## Summary table

| # | Feature (Element name) | Our component | Implementation approach (one line) | Effort | Hardest risk |
|---|---|---|---|---|---|
| 1 | Federation firewall (*Secure Border Gateway*) | **neuron-gateway** | Edge ASGI reverse proxy: allow/deny on X-Matrix `origin`, required client headers | **M** | Correct X-Matrix parsing/sig verify without breaking URIs |
| 2 | Advanced IAM (*GroupSync*) | **neuron-directory** | Reader (LDAP/SCIM)→desired-state; provisioner applies via Admin+CS API; lifecycle | **XL** | Idempotent reconciliation; safe deletion lifecycle |
| 3 | Audit logging (*AuditBot*) | **neuron-auditor** | E2EE-capable bot `/sync` → decrypt → JSON to FS/S3 | **L** | **E2EE forward-only decryption** (the headline risk) |
| 4 | Central supervision (*AdminBot*) | **neuron-supervisor** | Detect rooms → `make_room_admin`/join → moderate via CS+Admin API | **M** | Getting PL100 reliably; E2EE moderation |
| 5 | Admin console (*Element Admin*) | **neuron-console** | FastAPI + HTMX UI over Synapse Admin API; auth duality | **L** | MAS/MSC3861 auth duality; destructive-action safety |
| 6 | Media content scanner | **neuron-mediascan** | FastAPI proxy → ClamAV; cache; encrypted-media path | **M** | Encrypted-media decryption; authed-media endpoints |
| 7 | Scalability / HA (*Synapse Pro outcomes*) | **neuron-scale** | Stock Synapse workers + Redis + Postgres + reverse proxy | **L** | **Cannot match Pro's no-restart re-shard / Rust efficiency** |

---

## Per-feature detail

### 1 — neuron-gateway (Federation firewall)
- **Approach:** Reverse proxy at the edge. For federation requests, parse
  `Authorization: X-Matrix` and enforce an allow/deny list on `origin` (+ confirm
  `destination` is us); optional Ed25519 signature verification using the origin's server
  key. For client requests, enforce required-header rules. Reject with `403 M_FORBIDDEN`.
  Stream everything else through unchanged. In-protocol complement: optionally manage
  `m.room.server_acl` per room.
- **Open APIs used:** Server-Server API (`/_matrix/federation/...`), Key API
  (`/_matrix/key/v2/server`, `/query/{serverName}`), Client-Server (`/_matrix/client/...`),
  `.well-known/matrix/server`; `m.room.server_acl`.
- **OSS libs:** FastAPI/Starlette, httpx, `signedjson`/`unpaddedbase64`/`canonicaljson`
  (for X-Matrix signature checks), `cryptography`/PyNaCl (Ed25519). (Production path:
  nginx/HAProxy or Envoy `ext_authz`.)
- **Effort:** M.
- **Hardest risks:** parsing X-Matrix exactly per spec; **never canonicalizing the URI**
  (breaks signatures); avoiding lock-out of your own tooling when closing federation;
  `origin` is spoofable unless the signature is verified.

### 2 — neuron-directory (Advanced IAM / GroupSync)
- **Approach:** *Reader → desired-state → provisioner*, mirroring Element's
  Bridge/Provisioner split. Poll LDAP/AD (and accept SCIM push); build a normalized model
  of users, spaces, memberships, power levels; diff vs Synapse; apply via Admin API
  (user create/modify, force-join, deactivate+erase) and CS API (createRoom, `m.space.child`,
  `m.room.power_levels`). **Room cleanup** = idempotent post-pass. **Lifecycle** =
  lock → deactivate(grace, default 30d) → erase. Reconcile by immutable external id; manage
  local users only.
- **Open APIs used:** Synapse Admin API (`PUT /_synapse/admin/v2/users/{id}`,
  `/_synapse/admin/v1/deactivate|suspend|join|...`, lookups by external id / threepid);
  CS API (`createRoom`, membership, `m.room.power_levels`, spaces, `/rooms/{id}/hierarchy`).
- **OSS libs:** `ldap3`, FastAPI (SCIM intake), httpx, Pydantic; neuron-core admin client.
  (Optional: Microsoft Graph SDK for the Graph backend.)
- **Effort:** XL (largest feature: many backends, careful diffing, destructive lifecycle).
- **Hardest risks:** correct, *idempotent* reconciliation (avoid thrashing/loops in space
  trees); safe deletion (lock ≠ removal; grace-period erase); MSC3861/MAS disabling some
  admin endpoints; external-id immutability; "erase is not total" (media/messages remain).

### 3 — neuron-auditor (Audit logging)
- **Approach:** E2EE-capable bot with a persistent device + crypto store. Auto-join rooms;
  `/sync` for live events; decrypt where keys are held; write JSON events to filesystem
  (JSONL) or S3. Optionally import historical room keys via server-side key backup + SSSS.
  Optional appservice ingestion path for scale.
- **Open APIs used:** CS API (`/sync`, `/rooms/{id}/messages`, filters); full E2EE key
  management (`/keys/upload|query|claim|changes`, cross-signing, `/room_keys/...`, SSSS,
  to-device key sharing); AS API (`/_matrix/app/v1/transactions/{txnId}`) for the
  appservice path.
- **OSS libs:** **matrix-nio** (`olm` extra for E2EE) *or* mautrix-python; boto3 (S3);
  Pydantic. (Pantalaimon is an architectural reference for the proxy variant.)
- **Effort:** L.
- **Hardest risks (the headline of the whole project):** **E2EE is forward-only** — the
  bot cannot decrypt messages sent before it joined/was-known unless keys are shared or
  imported from backup; **trust bootstrapping** (compliant clients refuse to share keys
  with an unverified device → must set up cross-signing); the **plaintext audit store** is
  a concentrated, high-value target; appservice-E2EE MSCs (2409/3202) are experimental.

### 4 — neuron-supervisor (Central supervision)
- **Approach:** Privileged bot (own account/appservice). Detect new rooms (appservice
  events or polling `GET /_synapse/admin/v1/rooms`) → `make_room_admin` and/or admin
  force-join to obtain the highest available power. Moderate via CS API (power-levels,
  kick/ban, redact) and Admin API (delete/block room, purge history, quarantine media).
  Driven/visible through neuron-console's supervision tab. E2EE reuses neuron-auditor's
  crypto approach.
- **Open APIs used:** Synapse Admin API (`make_room_admin`, `/join`, room
  list/inspect/delete/block, purge history, redact, quarantine media); CS API
  (`m.room.power_levels`, kick/ban, redact).
- **OSS libs:** neuron-core admin client; matrix-nio (membership/moderation + E2EE); httpx.
- **Effort:** M.
- **Hardest risks:** "join every new room" is not one endpoint (needs detection + retries);
  `make_room_admin` can only *match*, not exceed, an existing PL100; `/join` needs the
  calling admin already in-room; E2EE needed to moderate encrypted content; some
  user-targeted admin endpoints disabled under MSC3861.

### 5 — neuron-console (Admin console)
- **Approach:** FastAPI backend wrapping the Synapse Admin API + operator auth; Jinja2 +
  HTMX UI (recommended) for users, rooms, media, event reports, statistics, registration
  tokens, server notices, and a pluggable supervision tab. Auth duality: static admin
  token **or** MAS OIDC session; under MSC3861 route disabled endpoints to MAS. Async
  status polling + confirmations for destructive actions.
- **Open APIs used:** the full Synapse Admin API surface (users, rooms, media, reports,
  statistics, registration tokens, server notices, purge); MAS admin API when delegated
  auth is on.
- **OSS libs:** FastAPI, Jinja2, HTMX, httpx, Authlib (OIDC), Pydantic; neuron-core.
  (Alternative: React/TypeScript SPA — heavier; OPEN-QUESTIONS Q3.)
- **Effort:** L (lots of CRUD surface; UI polish).
- **Hardest risks:** MAS/MSC3861 auth duality (which endpoints are disabled); destructive,
  irreversible, *asynchronous* operations (room delete/purge) need careful UX; admin-token
  must never reach the browser; pagination quirks / DB-heavy `order_by`.

### 6 — neuron-mediascan (Media content scanner)
- **Approach:** FastAPI proxy in front of Synapse's media repo, talking to a ClamAV daemon,
  with an in-process scan-result cache. Intercept downloads (authed v1 + legacy v3), fetch
  from Synapse, scan, serve-or-block (`403`). Support encrypted-media scanning (decrypt
  server-side from posted keys). Optional upload-time gate via a Synapse spam-checker
  module callback.
- **Open APIs used:** media upload (`/_matrix/media/v3/upload`, `/_matrix/media/v1/create`),
  authed download (`/_matrix/client/v1/media/...`), legacy download, federation media; the
  `check_media_file_for_spam` / media-repository module callbacks.
- **OSS libs:** FastAPI, httpx, `clamd`, vodozemac/python-olm (encrypted-media path),
  Pydantic. (Or run `matrix-content-scanner-python` as-is.)
- **Effort:** M.
- **Hardest risks:** encrypted-media means the proxy decrypts (sees plaintext) → key
  handling; the `/_matrix/media_proxy/unstable/...` convention is non-standard (clients
  must be pointed at it); authed-media vs deprecated unauthed endpoints differ by
  spec/Synapse version; stock Synapse has no built-in *download-time* scan hook.

### 7 — neuron-scale (Scalability / HA)
- **Approach:** A deployment blueprint using **stock** Synapse: `generic_worker` pool,
  dedicated media worker, federation senders, stream writers (events sharded by room id,
  multi-writer receipts/device_lists), Redis replication + shared cache, external
  PostgreSQL, a reverse proxy routing endpoint patterns to workers, `/health` checks, and
  autoscaling guidance. Distroless images.
- **Open APIs used / config:** Synapse worker config (`worker_app`, `instance_map`,
  `stream_writers`, `redis`, `federation_sender_instances`, `pusher_instances`,
  `enable_media_repo`), reverse-proxy routing of `/_matrix/...` patterns, PostgreSQL.
- **OSS libs/tools:** Synapse (stock), Redis, PostgreSQL, nginx/HAProxy, Docker/k8s.
- **Effort:** L (mostly config + docs + validation, but many moving parts).
- **Hardest risks (honest):** **stock Synapse must be restarted to re-shard** (no elastic
  no-restart scaling like Synapse Pro); **single-writer streams can't be load-balanced**;
  **Redis is a single pub/sub bus** (SPOF unless externally HA'd); Pro's **Rust multi-core
  + shared caches** mean stock needs more RAM/processes for equal throughput. We match the
  *outcome*, not the proprietary internals.

---

## Cross-cutting risks (apply to several features)

- **E2EE** (auditor, supervisor, mediascan-encrypted): forward-only decryption, trust
  bootstrapping, key custody. This is the single biggest technical risk and is treated in
  depth in `docs/feature-analysis.md` Feature 3.
- **MAS / MSC3861 delegated auth** (directory, console, supervisor): disables several
  Synapse admin endpoints; logic must branch on whether MAS is in use.
- **Synapse-internals limits** (scale): elastic re-sharding and Rust efficiency are
  proprietary; we approximate operationally and say so.
- **Reverse-proxy correctness** (gateway, mediascan, scale): never canonicalize URIs;
  keep `client_max_body_size` aligned with `max_upload_size`; pin `/sync` per user.
- **Clean-room discipline** (all): public sources only; no proprietary code/images; no
  trademarks; Synapse tree untouched.
