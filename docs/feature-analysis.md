# Neuron — Feature Analysis (ESS Pro → Clean-Room Equivalents)

> **Phase 1 deliverable.** This document is the documentation + spec analysis that
> underpins original, clean-room implementations of the features that distinguish
> **Element Server Suite (ESS) Pro** from **ESS Community**. It records, per feature:
> the exact *documented behavior*, the *configuration surface*, the *dependencies*,
> and the *precise open Matrix APIs/endpoints* needed to replicate the documented
> **functionality** (not Element's code).

## How to read this document

Each feature below has the same shape:

- **Documented behavior** — what Element's public docs say the feature does.
- **Config surface** — what an administrator configures.
- **Dependencies** — what the feature needs to run.
- **Open Matrix APIs / endpoints to replicate** — the standard, publicly specified
  endpoints (Matrix Client-Server, Server-Server, Application Service, and the open
  Synapse Admin API) we will build against. Exact paths are quoted.
- **Replication notes / risks** — honest caveats, including the E2EE audit problem
  and the limits of stock Synapse for HA.

---

## Clean-room methodology & sourcing

This analysis was produced under strict clean-room rules:

1. **Sources are public only.** We used (a) public Element documentation, (b) the
   open Matrix specification, and (c) open-source Matrix SDKs/projects and their
   docs. We did **not** read, copy, translate, or decompile Element's proprietary
   ESS Pro source code or container images.
2. **We replicate functionality, not expression.** Functionality is not
   copyrightable; Element's specific code is. Every design choice here derives from
   *documented behavior* and *open protocol endpoints*, never from their source.
3. **No Element/ESS trademarks** will be used in our component names, packages, or
   UI. This document refers to Element's features by their product names *only* to
   identify the behavior we are matching; our own components get original names
   (proposed in `ARCHITECTURE.md`).

### Source-access caveat (important for reproducibility)

This repository was analyzed inside a sandbox whose outbound network uses an
**allowlist proxy**. During research:

- `github.com`, `raw.githubusercontent.com`, and `pypi.org` were **reachable**.
- `docs.element.io`, `spec.matrix.org`, and `element-hq.github.io` returned
  **HTTP 403 (`host_not_allowed`)** to direct fetches.

Consequently:

- **Matrix spec** claims are cited to the spec's own source on GitHub
  (`raw.githubusercontent.com/matrix-org/matrix-spec/...`), which we fetched
  directly. This is the same normative text published at `spec.matrix.org`.
- **Synapse Admin API**, **worker/HA**, **reverse-proxy**, **media**, and
  **module-callback** claims are cited to the Synapse docs **bundled in this very
  repo** under `/home/user/Neuron/docs/...` (this repo *is* Synapse 1.155.0rc1).
- **Element/ESS Pro behavioral** claims (the proprietary features themselves) are
  cited to the Element doc URLs (`docs.element.io`, `ems-docs.element.io`,
  `element.io`) that were **surfaced via web search**. Because the live pages could
  not be fetched directly here, these should be **re-verified against the live docs**
  before implementation. They are marked where relevant.

No proprietary Element source code was accessed at any point.

---

## Feature 1 — Federation Firewall ("Secure Border Gateway" equivalent)

### Documented behavior

Element's **Secure Border Gateway (SBG)** is the federation-firewall feature shipped
with ESS Pro. It is documented as an **application-layer (HTTP) proxy/firewall** that
sits in front of the homeserver and filters/analyzes Matrix traffic between clients
and the homeserver, and between the homeserver and other federating homeservers.
[source: https://ems-docs.element.io/books/ems-knowledge-base/page/the-secure-border-gateway]
[source: https://docs.element.io/latest/element-server-suite-pro/introduction-to-ess-pro/]

"Secure Border Gateway" is a **product name, not a Matrix concept** — it is not
defined in the Matrix spec. Matrix.org characterizes an SBG generically as "an
application-layer firewall which intercepts APIs between Matrix components in order to
provide defence-in-depth or apply additional policy rules."
[source: https://matrix.org/blog/2025/06/demystifying-sbgs/]

Two primary documented capabilities:

1. **Enforcement of a private (closed) federation.** Homeservers outside the
   designated private federation cannot exchange data with servers inside it. With a
   Federation Allow List configured, non-allowed homeservers receive **HTTP 403** with
   a standard Matrix error body whose `errcode` is **`M_FORBIDDEN`**. The SBG decides
   based on the `Authorization` header in the request: for incoming requests it checks
   that the `origin` field matches one of the allowed remote server names (and the
   `destination` matches the local server name).
   [source: https://ems-docs.element.io/books/ems-knowledge-base/page/the-secure-border-gateway]

2. **Enforcement of client header parameters.** Admins can require that a Matrix
   client supply at least one configured header. For each header you set a name
   (case-insensitive) and a regex the value must match; a client that fails to supply
   a matching header is rejected with **HTTP 403** and a standard Matrix
   **`M_FORBIDDEN`** error.
   [source: https://ems-docs.element.io/books/ems-knowledge-base/page/the-secure-border-gateway]

Key documented benefit: **defence-in-depth** — the SBG prevents outside traffic from
even reaching the Synapse process.
[source: https://ems-docs.element.io/books/ems-knowledge-base/page/the-secure-border-gateway]

### Config surface

- **Install/enable** as an add-on from the ESS installer's Integrations page.
- **Federation Allow List** — a whitelist of remote homeserver **server names**. "Use
  the Allow List to restrict federation to the given whitelist of domains; if not
  specified, the default is to whitelist everything." To **fully close federation**,
  enable the Allow List with **no** allowed servers.
  [source: https://ems-docs.element.io/books/ems-knowledge-base/page/the-secure-border-gateway]
- **Required client headers** — zero or more `(header-name, value-regex)` pairs.
  Element warns that once any required client header is defined, the bundled Element
  Web and the installer "Admin" tab stop connecting (they don't send the header).
  [source: https://ems-docs.element.io/books/ems-knowledge-base/page/the-secure-border-gateway]
- **Mirrors Synapse's own allow list.** If a Federation Allow List is configured in
  Synapse, the SBG enforces the same private federation. The underlying open Synapse
  setting is **`federation_domain_whitelist`** (absent ⇒ federate with all). Gotcha:
  your own `server_name` must be in the list.
  [source: https://matrix-org.github.io/synapse/latest/usage/configuration/config_documentation.html]
  [source: https://github.com/matrix-org/synapse/issues/6635]

### Dependencies

- A Matrix homeserver behind it (the SBG proxies; it does not replace the HS).
- TLS termination / reverse-proxy placement on the client port (443) and federation
  port (8448), since it must read the `Authorization: X-Matrix` header.
  [source: /home/user/Neuron/docs/reverse_proxy.md]
- Federation server-name discovery via standard Matrix discovery: `.well-known/matrix/server`
  (`m.server`), then SRV `_matrix-fed._tcp.<hostname>` (deprecated `_matrix._tcp.`),
  then A/AAAA on port 8448.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/server-server-api.md]
  [source: /home/user/Neuron/docs/delegate.md]
- Server-key access for signature verification: `/_matrix/key/v2/server` and the
  notary `/_matrix/key/v2/query/{serverName}`.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/server-server-api.md]

### Open Matrix APIs / endpoints to replicate

A reverse proxy reproducing the SBG must route and inspect these **documented** paths.
Per Synapse's reverse-proxy guide, federation/client traffic lives under `/_matrix`
(plus Synapse client helpers under `/_synapse/client`); the proxy **must not
canonicalize/normalize the URI** (no `%xx` decoding) or it breaks X-Matrix signature
verification. [source: /home/user/Neuron/docs/reverse_proxy.md]

**Server-Server (federation) — inspect `origin`/`destination`, enforce allow list:**
[source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/server-server-api.md]
- `GET /_matrix/federation/v1/version`
- `PUT /_matrix/federation/v1/send/{txnId}` (PDUs + EDUs; spec limit ≤ 50 PDUs, ≤ 100 EDUs/txn)
- `GET /_matrix/federation/v1/make_join/...`, `PUT /_matrix/federation/v1/send_join/...`, `PUT /_matrix/federation/v2/send_join/...`
- `GET /_matrix/federation/v1/make_leave/...`, `PUT /_matrix/federation/v1/send_leave/...`, `PUT /_matrix/federation/v2/send_leave/...`
- `GET /_matrix/federation/v1/make_knock/...`, `PUT /_matrix/federation/v1/send_knock/...`
- `PUT /_matrix/federation/v1/invite/...`, `PUT /_matrix/federation/v2/invite/...`
- `GET /_matrix/federation/v1/backfill/...`, `GET /_matrix/federation/v1/event_auth/...`
- `GET /_matrix/federation/v1/state/...`, `GET /_matrix/federation/v1/state_ids/...`
- `POST /_matrix/federation/v1/user/keys/query`

**Key API (signature-verification dependency):**
- `GET /_matrix/key/v2/server`
- `GET /_matrix/key/v2/query/{serverName}`

**Client-Server (for client-header enforcement):** all `/_matrix/client/...` (plus
`/_synapse/client/...` in Synapse). The proxy inspects inbound headers and rejects
requests lacking a required header with `403 M_FORBIDDEN`.
[source: /home/user/Neuron/docs/reverse_proxy.md]

**Validating federation requests at the proxy (X-Matrix header):**
[source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/server-server-api.md]
- Federation requests carry `Authorization: X-Matrix origin="...",destination="...",key="ed25519:key1",sig="..."`.
- The signed object is canonical JSON containing: `method`, `uri` (path **including
  query string**), `origin`, `destination`, and `content` (parsed body if any).
  Signature is Ed25519 over that object.
- Minimal proxy enforcement: parse the X-Matrix params, confirm `origin` is allowed
  (cheap, no crypto), confirm `destination` is your server name. Full validation
  additionally fetches the origin's Ed25519 key and verifies `sig`.

### In-protocol alternative (`m.room.server_acl`)

The spec offers a per-room, in-protocol alternative: the **`m.room.server_acl`** state
event. Servers MUST prevent denied servers from participating when present.
[source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/modules/server_acls.md]
- `allow`: case-insensitive **glob** patterns vs server names (port excluded). Absent ⇒
  deny all (including sender).
- `deny`: glob patterns; absent ⇒ deny none. **Deny precedes allow.**
- `allow_ip_literals`: bool, default `true`; `false` ⇒ reject IP-literal names.
- Evaluation: no event ⇒ allow → IP-literal & `allow_ip_literals:false` ⇒ deny →
  matches `deny` ⇒ deny → matches `allow` ⇒ allow → else deny.
- Glob: `*` = zero+ chars, `?` = exactly one char.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/appendices.md]
- Warning: omitting an `allow` rule bans all servers including your own; common pattern
  is `allow: ["*"]` + curated `deny`. CIDR/ports unsupported.

### Replication notes / risks

- **`server_acl` is per-room and cooperative; a firewall is server-wide and enforced.**
  ACLs depend on every remote server honoring them; a proxy enforces at your own edge.
  A clean-room SBG should reject at the proxy for hard guarantees.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/modules/server_acls.md]
- **Do not canonicalize URIs at the proxy** or X-Matrix signatures break (Synapse docs
  flag nginx `proxy_pass` trailing slashes and require Apache `nocanon` /
  `AllowEncodedSlashes NoDecode`). [source: /home/user/Neuron/docs/reverse_proxy.md]
- **`origin` is attacker-supplied until verified.** Allow-list matching on `origin`
  alone is spoofable; a true security boundary verifies the Ed25519 `sig`. Element's
  docs describe matching `origin`; they do not document signature-verification depth,
  so treat full crypto verification as our own design decision.
- **Closing federation can lock out your own tooling** (Element warns required client
  headers break Element Web / Admin tab). Exempt trusted internal origins/paths (e.g.
  `/_matrix/key/v2/server`, `/_matrix/federation/v1/version` for discovery/health).
- **Include your own `server_name`** in any allow list (both the proxy and Synapse's
  `federation_domain_whitelist`).
- **"SBG" is a product name, not a wire protocol.** The clean-room implementation is
  purely an HTTP reverse proxy applying allow/deny + header rules over open `/_matrix/...`
  and `/_matrix/key/...` endpoints. [source: https://matrix.org/blog/2025/06/demystifying-sbgs/]

---

## Feature 2 — Advanced IAM / Directory Sync ("GroupSync" equivalent)

### Documented behavior

**Purpose & model.** Advanced Identity Management (formerly "Group Sync") "allows you
to represent your organization's structure within Matrix and Element: creating a space
for all its members, maintaining their membership in rooms and subspaces, managing
power levels and more," and lets you "use the ACLs from your identity infrastructure in
order to set up permissions on Spaces and Rooms."
[source: https://element.io/en/server-suite/advanced-identity-access-management]
[source: https://element.io/en/server-suite/pro]

**Two-component architecture: Bridge + Provisioner.** "Bridges' job is to turn the
contents of an external data directory into a data structure that can then be
constructed on the Matrix server by the Provisioner." Bridges run continuously and
trigger provisioning on startup or whenever they observe changes. The Provisioner
"takes the directory produced by a bridge, maps it to Matrix spaces (see Space
mapping), and enforces its presence on a Matrix server by creating and modifying it as
needed."
[source: https://docs.element.io/latest/element-server-suite-pro/advanced-identity-management/bridging/]

**Per-homeserver ownership (federation).** "Each federated server maintains its own
Advanced Identity Management instance... Each Provisioner is responsible for managing
users belonging to its homeserver, and ignores those that belong to another
homeserver."
[source: https://docs.element.io/latest/element-server-suite-pro/advanced-identity-management/bridging/]

**Directory backends.** "Advanced IAM supports LDAP/AD, Microsoft Graph and SCIM user
backends."
[source: https://element.io/en/server-suite/advanced-identity-access-management]

**Group → space/room mapping.** Groups/departments/roles are mirrored to room/space
memberships and permissions; names/emails/avatars stay in sync. A root LDAP container
(Base DN / root OU) "becomes the root space"; "Spaces are mapped against Org Units, but
you can map a space against any object."
[source: https://docs.element.io/ess-classic-lts-24.10/element-server-suite-classic/integrations/setting-up-group-sync-with-the-installer/]

**Attribute mapping.** A username attribute (e.g. `sAMAccountName`, configured as
`uid`) maps to the MXID localpart; a name attribute gives an internal ID to the space.
[source: https://docs.element.io/ess-classic-lts-24.10/element-server-suite-classic/integrations/setting-up-group-sync-with-the-installer/]

**Power-level mapping.** Map an external ID (LDAP DN) to a power level; "every user
belonging to this external ID is granted the power level set." Documented: PL 50 =
moderator; PL 100 = admin but **not** mapped (GroupSync manages spaces/invites itself);
"Custom power levels other than 0 and 50 are not supported yet."
[source: https://docs.element.io/ess-classic-lts-24.10/element-server-suite-classic/integrations/setting-up-group-sync-with-the-installer/]

**Room cleanup.** "After each provisioning cycle, Advanced Identity Management will
clean up the rooms" — a reconciliation post-pass each cycle.
[source: https://docs.element.io/latest/element-server-suite-pro/advanced-identity-management/room-cleanup/]

**Deprovisioning / deletion lifecycle (soft-delete with grace period):**
1. *Lock* — undesirable user's account is locked (no login), **but** they are not
   removed from rooms and their power levels stay the same.
2. *Deactivate with delayed erasure* — removed-from-directory users are deactivated,
   erasure delayed by a grace period; re-adding before expiry unlocks them. Grace
   format `<amount><unit>` with unit `s|m|h|d` (e.g. `24h`, `31d`); default 30 days.
3. *Erase* — once the grace period expires, the user is erased, "done under the hood by
   the Synapse API deactivate user, with erase set to true."
   [source: https://docs.element.io/latest/element-server-suite-pro/advanced-identity-management/user-deletion/]

### Config surface

YAML, under a `groupSync` section in the deployment values. Documented concepts:
- **Source block** — `source.type` (e.g. `ldap`), a `checkInterval` (seconds), LDAP
  `uri`, a base OU that "becomes the root space."
- **LDAP connection** — Base DN, Bind DN, Bind Password, Filter, URI, check interval.
- **Attribute mapping** — `uid` (e.g. `"sAMAccountName"`) → localpart; a `name`
  attribute → space names.
- **Space mapping** — spaces each with `id`, `name`; `groups` with `externalId`;
  `subspaces`; a `powerLevel` (e.g. `50`). "Space mapping… is optional."
- **User-deletion grace period** — `<amount><unit>`, default `30d`.
- **Bridging targets** — list of homeserver targets to provision into.
  [source: https://docs.element.io/ess-classic-lts-24.10/element-server-suite-classic/integrations/setting-up-group-sync-with-the-installer/]
  [source: https://docs.element.io/latest/element-server-suite-pro/advanced-identity-management/user-deletion/]

### Dependencies

- A directory source: LDAP/AD, Microsoft Graph, or SCIM.
- The **Synapse Admin API** for the lifecycle/erasure step and forced membership.
  Erasure "is done under the hood by the Synapse API deactivate user, with erase set to
  true." [source: /home/user/Neuron/docs/admin_api/user_admin_api.md]
- **Matrix Authentication Service (MAS)** — ESS's OAuth2/OIDC auth layer (MSC3861).
  With MSC3861 enabled, several legacy Synapse admin endpoints (reset_password,
  set-admin, login-as-user, guests filter) are disabled.
  [source: https://github.com/element-hq/matrix-authentication-service]
  [source: https://github.com/matrix-org/synapse/pull/15582]
- **SCIM** provisioning (RFC 7643/7644); a subset exists in Synapse via MSC4098.
  [source: https://github.com/element-hq/synapse/pull/17144]

### Open Matrix APIs / endpoints to replicate

**Identity resolution (directory ID → MXID):** mirror Synapse SSO mapping-provider
semantics (`get_remote_user_id` → immutable external id; `map_user_attributes` →
localpart/display_name/picture/emails). [source: /home/user/Neuron/docs/sso_mapping_providers.md]
- `GET /_synapse/admin/v1/auth_providers/{provider}/users/{external_id}` — resolve MXID
  by external IdP id.
- `GET /_synapse/admin/v1/threepid/{medium}/users/{address}` — resolve MXID by
  email/msisdn. [source: /home/user/Neuron/docs/admin_api/user_admin_api.md]
- SSO login discovery: `GET /_matrix/client/v3/login` (`m.login.sso`, `m.login.token`),
  `GET /_matrix/client/v3/login/sso/redirect[/{idpId}]`.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/modules/sso_login.md]
- JWT login (non-standard): `org.matrix.login.jwt` (localpart from `sub`).
  [source: /home/user/Neuron/docs/jwt.md]

**User provisioning / profile sync / admin flag (Synapse Admin API):**
- `PUT /_synapse/admin/v2/users/{userId}` — create or modify (idempotent: 201/200).
  Sets `displayname`, `avatar_url`, `threepids`, `external_ids` (SSO linkage),
  `admin`, `deactivated`, `locked`, `user_type`. **This single endpoint covers profile
  sync + external-id linkage + lock + admin flag.**
- `GET /_synapse/admin/v2/users/{userId}`, `GET /_synapse/admin/v2/users` (and v3) —
  query/list (pagination, filters incl. `deactivated`/`admins`/`locked`) to diff
  directory vs server.
- `PUT /_synapse/admin/v1/users/{userId}/admin` (disabled under MSC3861 — prefer the v2
  `admin` field).
- `GET /_synapse/admin/v1/users/{userId}/joined_rooms`, `GET .../memberships`.
  [source: /home/user/Neuron/docs/admin_api/user_admin_api.md]

**Forced membership & CS membership ops:**
- `POST /_synapse/admin/v1/join/{room_id_or_alias}` with `{"user_id": ...}` — admin
  force-join a **local** user (admin must be in room with invite permission). The
  "enforce membership" primitive. [source: /home/user/Neuron/docs/admin_api/room_membership.md]
- CS: `POST /_matrix/client/v3/rooms/{roomId}/invite|join|kick|ban|forget`,
  `GET /_matrix/client/v3/rooms/{roomId}/members`.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/_index.md]

**Space/room creation & structure:**
- `POST /_matrix/client/v3/createRoom` — for a space set `creation_content.type =
  m.space`, seed `initial_state`, set `power_level_content_override`.
- Space type `m.space`; parent→child `m.space.child` (state_key = child room id; fields
  `via`, `order`; unlink by omitting `via`); child→parent `m.space.parent`. Loops
  disallowed.
- `GET /_matrix/client/v1/rooms/{roomId}/hierarchy` — read the space tree for
  reconciliation.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/modules/spaces.md]

**Power-level / permission assignment:**
- `PUT /_matrix/client/v3/rooms/{roomId}/state/m.room.power_levels` — write target
  MXIDs into the `users` map (e.g. 50). Fields: `users_default`, `events`,
  `events_default`, `state_default`, `ban`, `kick`, `invite`, `redact`.
- Initial levels at creation via `power_level_content_override`.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/_index.md]

**Deprovisioning / deletion lifecycle:**
- *Lock*: `PUT /_synapse/admin/v2/users/{userId}` `{"locked": true}` (re-enable `false`).
- *Reactivate within grace*: unset `locked`/`deactivated` via same PUT.
- *Erase after grace*: `POST /_synapse/admin/v1/deactivate/{userId}` `{"erase": true}`
  — removes tokens/devices/3PIDs, removes from all rooms, rejects pending invites,
  GDPR-marks the user. (Matches "deactivate with erase=true".)
- Optional content moderation on removal: `POST /_synapse/admin/v1/user/{userId}/redact`
  (+ `GET /_synapse/admin/v1/user/redact_status/{redact_id}`);
  `PUT /_synapse/admin/v1/suspend/{userId}`.
  [source: /home/user/Neuron/docs/admin_api/user_admin_api.md]
  [source: https://docs.element.io/latest/element-server-suite-pro/advanced-identity-management/user-deletion/]

**Room cleanup (post-cycle reconciliation):** diff desired structure
(`GET .../hierarchy` + `GET .../joined_rooms`) against the directory; remove stale links
by writing `m.space.child` with no `via`; remove members via CS `/kick`/`/ban`; full
teardown via the room delete/purge Admin API (`docs/admin_api/rooms.md`).

**Bulk-provisioning rate limits:** `POST/GET/DELETE
/_synapse/admin/v1/users/{userId}/override_ratelimit` (set both to 0) for the service
account. [source: /home/user/Neuron/docs/admin_api/user_admin_api.md]

### Replication notes / risks

- **Lock ≠ removal.** On lock, leave memberships and PLs intact (only login blocked);
  strip rooms/PLs only on erase.
- **Per-homeserver scoping.** Manage only local users; ignore remote/foreign-provisioner
  users to avoid conflicting writes in federated deployments.
- **MSC3861/MAS interaction.** When delegating auth to MAS, `reset_password`,
  `users/{id}/admin`, `users/{id}/login`, and the `guests` filter are disabled. Use the
  v2 `admin` field; password management belongs to MAS.
  [source: https://github.com/matrix-org/synapse/pull/15582]
- **Power-level expressiveness.** Match documented behavior by restricting group→PL
  mapping to 0/50 (100 intentionally unmapped), even though the API allows arbitrary
  levels.
- **External-id immutability.** Bind directory unique key → MXID create-once; reconcile
  by external id, not display attributes. [source: /home/user/Neuron/docs/sso_mapping_providers.md]
- **Erase is not total.** Synapse deactivate+erase does not remove SSO mappings,
  uploaded media, messages, or registration timestamp. Stronger erasure needs
  supplementary calls (e.g. `DELETE /_synapse/admin/v1/users/{userId}/media`, redaction).
- **Cleanup ordering.** Implement cleanup as an idempotent post-pass that reconciles to
  desired state, not per-event reactions, to avoid thrashing.

---

## Feature 3 — Audit Logging ("AuditBot" equivalent)

### Documented behavior

- AuditBot is a dedicated bot user **invited into every room** that **writes raw events
  to configured outputs** as they occur.
  [source: https://docs.element.io/latest/element-cloud-documentation/integrations/audit-bot/]
- Purpose is compliance/audit: export communications in any room the bot is a member of,
  **even if encryption is in use** — framed as meeting compliance requirements.
  [source: https://docs.element.io/latest/element-server-suite-classic/integrations/setting-up-adminbot-and-auditbot/]
  [source: https://ems-docs.element.io/integrations/Audit-Bot.html]
- Configurable to audit rooms and optionally private (direct) messages.
  [source: https://ems-docs.element.io/integrations/Audit-Bot.html]
- It **reads/decrypts** encrypted messages; can write all decrypted events to an
  S3-compatible store as a **continuous export** that starts when a bucket is configured
  and stops when cleared.
  [source: https://ems-docs.element.io/integrations/Audit-Bot.html]
- It is the read-only counterpart to **AdminBot**; the pair is deployed together.
- **Security note:** the stored logs are **not encrypted at rest** — anyone with store
  access can read all logged content.
  [source: https://ems-docs.element.io/integrations/Audit-Bot.html]

### Config surface & outputs

- **Output format:** machine-readable events in **JSON**.
  [source: https://docs.element.io/latest/element-server-suite-pro/introduction-to-ess-pro/]
- **Output destinations:** **filesystem** or **S3-compatible** object storage.
  [source: https://ems-docs.element.io/integrations/Audit-Bot.html]
- **Outputs** are an explicit config concept; S3 export starts by configuring a bucket
  and stops by clearing it.
- **Scope toggle:** rooms, and optionally private messages.
- **Deployment:** Helm values (ESS Pro is Helm-based).
- **Persistent state:** a PVC holding the bot's decryption keys and a cache of the bot's
  logins. [source: https://ems-docs.element.io/books/element-server-suite-classic-documentation-lts-2404/page/backup-and-restore]
- **Recovery/backup phrase:** a Secure Backup Phrase unlocks Secure Storage and encrypted
  messages on a new session (relevant: the bot obtains keys via secure backup).
- **Retention:** no documented in-bot retention; effectively delegated to the destination
  store (S3 lifecycle / filesystem rotation).

### Dependencies

- A Synapse homeserver + a way to auto-invite the bot into rooms.
- A persistent volume for crypto keys + login cache.
- An output sink: filesystem or S3 + credentials.
- For an open re-implementation, the E2EE work maps to **Pantalaimon** (an E2EE-adding
  reverse-proxy daemon) [source: https://github.com/matrix-org/pantalaimon], or a
  crypto-capable SDK: **matrix-nio** (E2EE extra, needs libolm)
  [source: https://matrix-nio.readthedocs.io/en/latest/nio.html] or **mautrix-python**
  (built-in Olm/Megolm + appservice/Intent) [source: https://github.com/mautrix/python].

### Open Matrix APIs / endpoints to replicate

**Reading the timeline (the audit "tap"):**
- `GET /_matrix/client/v3/sync` — long-poll; response `rooms` split into
  `join`/`invite`/`leave` (each with `timeline`, `state`, `account_data`), plus
  `to_device`, `device_lists`, `device_one_time_keys_count`. Params: `filter`, `since`,
  `timeout`, `full_state`, `set_presence`.
- `GET /_matrix/client/v1/rooms/{roomId}/messages` — paginate history (`from`, `to`,
  `dir`, `limit`, `filter`).
- `POST /_matrix/client/v3/user/{userId}/filter` — create a filter to scope events.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/_index.md]

**Appservice ingestion path (alternative to a single bot account):**
- `PUT /_matrix/app/v1/transactions/{txnId}` — HS pushes an `events` array; `txnId`
  dedups on retry.
- Registration: `id`, `as_token`, `hs_token`, `sender_localpart`, `namespaces`
  (`users`/`aliases`/`rooms`, each regex + `exclusive`).
- Masquerade: authenticate with `as_token`, pass `user_id` query param (must be in the
  `users` namespace).
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/application-service-api.md]
- Appservice E2EE delivery is via MSCs: **MSC2409** delivers to-device/EDUs
  (`receive_ephemeral: true`; Synapse `msc2409_to_device_messages_enabled`); **MSC3202**
  adds `device_lists`, OTK counts, fallback-key state (`org.matrix.msc3202: true`;
  Synapse `msc3202_transaction_extensions`).
  [source: https://github.com/matrix-org/matrix-spec-proposals/pull/3202]

**Full E2EE key-management endpoints to replicate:**
[source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/modules/end_to_end_encryption.md]
- `POST /_matrix/client/v3/keys/upload` — device identity keys + one-time keys.
- `POST /_matrix/client/v3/keys/query` — fetch others' device keys.
- `POST /_matrix/client/v3/keys/claim` — claim OTKs to set up Olm sessions.
- `GET /_matrix/client/v3/keys/changes` — device-list changes between sync tokens.
- Algorithms: **Olm** `m.olm.v1.curve25519-aes-sha2` (1:1 to-device) and **Megolm**
  `m.megolm.v1.aes-sha2` (room messages). Room encryption state: `m.room.encryption`.
- Key-sharing to-device events: `m.room_key`, `m.forwarded_room_key`,
  `m.room_key_request` (`action` = `request` / `request_cancellation`).
- Cross-signing: `POST /_matrix/client/v3/keys/device_signing/upload`,
  `POST /_matrix/client/v3/keys/signatures/upload`.
- Server-side key backup (`m.megolm_backup.v1.curve25519-aes-sha2`):
  `POST/GET /_matrix/client/v3/room_keys/version`,
  `DELETE /_matrix/client/v3/room_keys/version/{version}`,
  `PUT/GET /_matrix/client/v3/room_keys/keys`.
- SSSS (account data + to-device for cross-signing/backup secrets):
  `m.secret_storage.default_key`, `m.secret_storage.key.[keyId]`, algorithm
  `m.secret_storage.v1.aes-hmac-sha2`; secret sharing via to-device `m.secret.request` /
  `m.secret.send`; passphrase keys via PBKDF2 (`m.pbkdf2`).
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/modules/secrets.md]
- Device management: `GET /_matrix/client/v3/devices`,
  `GET/PUT/DELETE /_matrix/client/v3/devices/{deviceId}`.
  [source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/modules/device_management.md]

### E2EE challenge analysis & proposed approach

The hard requirement: in an encrypted room, bodies are wrapped in **Megolm**; a device
can only decrypt if it holds the **inbound Megolm group session** for that room+sender.
Sessions are distributed only to devices a sender chooses to share with, over
Olm-encrypted `m.room_key` to-device events. So the bot must be a *recipient senders
agree to share keys with* (a trusted/verified member device), or obtain keys
out-of-band. Honest options:

1. **Bot as a verified member receiving live Megolm keys (recommended baseline).** The
   bot logs in as a normal device, uploads device + OTKs (`/keys/upload`), and is
   invited into rooms. When it is a known member at send time, clients distribute room
   keys via `m.room_key`. To be *trusted* enough that compliant clients share keys, set
   up **cross-signing** and verify the device.
   - **Central honest limitation:** key sharing is **forward-only** — a member only gets
     sessions for messages sent **after** it joined and was known to senders.
     **Historical / pre-join messages cannot be decrypted** without explicit key sharing
     or import (the classic matrix-nio "no session found" failure).
     [source: https://github.com/matrix-org/pantalaimon/issues/87]
   - Verification is normally interactive (SAS), awkward for a bot; cross-signing setup
     matters in practice.

2. **Server-side key backup + SSSS (closes the gap where keys exist).** If room keys are
   backed up to `/_matrix/client/v3/room_keys/...` and the bot recovers the
   backup-decryption secret from SSSS (unlocked by a recovery passphrase via PBKDF2 /
   `m.secret.request`), the bot can **import historical sessions** it would otherwise
   lack. ESS's documented Secure Backup Phrase + AuditBot "decryption keys" PVC line up
   with this. **Trade-off:** only recovers keys that were actually backed up; requires
   custody of a high-value recovery secret.

3. **Appservice + masquerade (MSC2409/MSC3202).** Register an appservice receiving every
   event via `PUT /_matrix/app/v1/transactions/{txnId}` over broad namespaces. With
   MSC2409/MSC3202 the appservice can participate in E2EE. **Trade-off:** unstable MSCs +
   experimental flags; masquerade lets you *act as* users but does **not** grant their
   keys — the appservice still needs its own crypto devices to receive keys. This is a
   scale/ingestion layer on top of option 1/2, not a shortcut.

4. **Pantalaimon-style E2EE proxy (clean separation).** Run a crypto-aware proxy that
   owns Olm/Megolm and exposes a decrypted CS API to a crypto-free audit consumer.
   **Trade-off:** still subject to the same key-acquisition reality; relocates complexity.

**Proposed approach:** Use a crypto-capable SDK (matrix-nio with libolm, or
mautrix-python's crypto helper — `import_keys`/`export_keys` cover Megolm
backup import/export). Establish a persistent device with stable `device_id` and
persistent crypto store (mirrors the ESS PVC); set up cross-signing and verify so
clients share keys (option 1). Layer in **key-backup recovery via SSSS** (option 2) to
ingest historical sessions where available. Auto-invite the bot to all rooms (or use the
appservice path, option 3, at scale). Decrypt in-process and stream decrypted event JSON
to a filesystem or S3 sink. **Document loudly** that (a) pre-join/never-shared messages
are undecryptable, and (b) the output store is plaintext and must be access-controlled
(matching Element's own warning).

### Replication notes / risks

- **Forward-only decryption is unavoidable** at the protocol level; surface "could not
  decrypt (no session)" rather than silently dropping events; record the encrypted
  envelope + failure reason for completeness.
- **Trust bootstrapping is the practical blocker:** without cross-signing/verification,
  compliant clients legitimately refuse to share keys. Budget for verification automation.
- **Security/governance:** the bot reads effectively all rooms, holds a recovery secret,
  and writes a plaintext store — a concentrated, high-value target. Encrypt the sink,
  restrict access, treat the recovery passphrase as top-tier.
- **MSC instability:** appservice E2EE relies on experimental Synapse flags and pre-spec
  field names; pin to a known-good Synapse.
- **Clean-room compliance:** all mechanisms are documented in the open spec, public
  Element docs, and OSS projects; do not port Element's proprietary AuditBot source.

---

## Feature 4 — Central Supervision ("AdminBot" equivalent)

### Documented behavior

- ESS Pro's "AdminBot" (renamed **"supervision"** in recent values files) is a
  **privileged service account** that "works in addition to the EMS Server Admin UI and
  Synapse Admin API," for centralized moderation/supervision of every room.
  [source: https://docs.element.io/latest/element-cloud-documentation/integrations/admin-bot/]
- Core mechanism: most room admin tasks require a local account with PL 100 in the room;
  the AdminBot extension "ensures this by **inviting and promoting the account `adminbot`
  in every Matrix room created on your server**."
- Capabilities: moderate content, invite/promote members, kick/ban — designed to regain
  control when all admins depromoted themselves, and to address CoC violations.
- Encryption: "Admin Bot is able to **read encrypted messages** to allow you to moderate
  messages." First login from a new browser requires the Secure Backup Phrase.
  [source: https://ems-docs.element.io/books/element-cloud-documentation/page/admin-bot]
- Relationship to Element Admin: the console has an **"Admin Bot" tab** with a "Log in as
  Admin bot" button plus key-backup credentials. The bot is the privileged actor; the
  console drives/impersonates it.
  [source: https://docs.element.io/latest/element-server-suite-classic/administration/using-the-admin-console/]

### Config surface

- Top-level values key historically `adminbot`, now **`supervision`**.
- Wired in as an **application service** ("Automatically compute the appservice tokens").
- A **Secure Backup Phrase** to access the key backup store (so it can decrypt history).
- A persistent volume (the "AdminBot PVC") storing the bot's decryption keys and login
  cache; sensitive ("any user accessing it could read the content of your organization
  rooms"); "revoking the bot tokens" disables login-as-AdminBot.
  [source: https://ems-docs.element.io/books/element-cloud-documentation/page/admin-bot]

### Dependencies

- Synapse with the **Synapse Admin API** enabled + a server-admin token.
- Application-service registration to own the `adminbot`/supervision identity.
- E2EE key backup / Secure Storage (cross-signing + recovery passphrase).
- Persistent storage for keys + login cache.

### Open Synapse Admin API endpoints to replicate

The documented "invite + promote to PL100 in every room, then moderate" maps to:
- **Grant the bot room-admin power** (the linchpin): `POST
  /_synapse/admin/v1/rooms/<room_id_or_alias>/make_room_admin` with `{"user_id":
  "@adminbot:server"}` — "Grants another user the highest power available to a local user
  who is in the room. If the user is not in the room, and it is not publicly joinable,
  then invite the user." [source: /home/user/Neuron/docs/admin_api/rooms.md]
- **Force the bot into a room:** `POST /_synapse/admin/v1/join/<room_id_or_alias>` with
  `{"user_id": "@adminbot:server"}` (calling admin must be in the room with invite
  permission; local users only). [source: /home/user/Neuron/docs/admin_api/room_membership.md]
- **Enumerate rooms:** `GET /_synapse/admin/v1/rooms` (`search_term`, `order_by`,
  `from`, `limit`).
- **Inspect a room:** `GET /_synapse/admin/v1/rooms/<room_id>`, `.../members`,
  `.../state`, `.../messages`, `.../hierarchy`, `.../context/<event_id>`.
- **Redact a user's events:** `POST /_synapse/admin/v1/user/<user_id>/redact` (+ `GET
  /_synapse/admin/v1/user/redact_status/<redact_id>`).
- **Shut down / delete / block a room:** `DELETE /_synapse/admin/v2/rooms/<room_id>`
  (async; status `.../delete_status` and `/_synapse/admin/v2/rooms/delete_status/<delete_id>`),
  v1 sync variant `DELETE /_synapse/admin/v1/rooms/<room_id>`, and `PUT
  /_synapse/admin/v1/rooms/<room_id>/block`.
- **Purge history:** `POST /_synapse/admin/v1/purge_history/<room_id>[/<event_id>]` (+
  status). [source: /home/user/Neuron/docs/admin_api/purge_history_api.md]
- **Quarantine media in a room:** `POST /_synapse/admin/v1/room/<room_id>/media/quarantine`.
  [source: /home/user/Neuron/docs/admin_api/media_admin_api.md]

CS-API endpoints the bot uses **as a room member** once it holds PL100: `PUT
/_matrix/client/v3/rooms/{roomId}/state/m.room.power_levels` (promote/demote),
`POST .../kick|ban|invite`, and `PUT /_matrix/client/v3/rooms/{roomId}/redact/{eventId}/{txnId}`.

### Replication notes / risks

- **"Join every newly created room" is not one endpoint.** Detect room creation (an
  appservice/bot listening for `m.room.create`, or polling `GET /_synapse/admin/v1/rooms`)
  then call `make_room_admin` (and/or `/join`).
- `make_room_admin` only grants "the highest power available to a local user who is in
  the room" — in a room where another member holds PL100 it can match but not exceed.
- `/join` requires the calling admin already in the room with invite permission and only
  works for local users; prefer `make_room_admin` for arbitrary rooms.
- E2EE: the admin API alone cannot decrypt; reading encrypted content needs a real device
  with cross-signing/key-backup access. Treat the recovery passphrase + key PVC as
  top-secret; support token revocation.
- Several user-targeted endpoints (reset_password, set-admin, shadow-ban, login-as-user)
  are **disabled under MSC3861/MAS** — supervision logic must branch on that.
- Clean-room: replicate the *outcome* (a PL100 bot in all rooms via open admin/CS
  endpoints); do not copy Element's appservice schema or values keys beyond publicly
  documented names.

---

## Feature 5 — Admin Console ("Element Admin" equivalent)

### Documented behavior

- **Element Admin** is a web-based administration panel for ESS, in both Pro and
  Community editions; "a single-page React application which can be deployed in any
  static hosting service or container environment."
  [source: https://github.com/element-hq/element-admin]
- The classic Admin Console is "a graphical interface for administering your homeserver
  and the Element Server Suite," with documented tabs:
  [source: https://docs.element.io/latest/element-server-suite-classic/administration/using-the-admin-console/]
  - **User management** — "Add User" tab; click an account to manage it; a "make a user
    a Synapse admin" checkbox.
  - **Room management** — "Rooms" tab listing room id, name, #users, room version; delete
    rooms; per-room management view.
  - **Server Info** — installed Synapse version + homeserver Python version.
  - **Admin Bot** tab — "Log in as Admin bot" button + key-backup credentials (ties to
    Feature 4).

### Config surface

- Requires a domain with valid HTTPS; must be served from a secure context (next-gen
  auth APIs require it).
- Must be pointed at: a Synapse instance with admin API accessible, and a MAS instance
  with its admin API accessible.
- License: dual AGPLv3 OR Element Commercial (AGPL portion is open for clean-room study,
  must not be copied into a proprietary re-implementation).
  [source: https://github.com/element-hq/element-admin]

### Dependencies

- Synapse with the **Synapse Admin API** + a server-admin credential.
- **Matrix Authentication Service (MAS)** for next-gen/delegated auth; auth uses the MAS
  admin API + an admin-scoped OIDC session rather than a static Synapse admin token.
- HTTPS / secure-context hosting (browser SPA).
- **MSC3861 interaction:** with MAS on, several legacy Synapse admin endpoints
  (reset_password, set/get admin, login-as-user, shared-secret register, account
  validity) are disabled — a MAS-backed console must use MAS's admin API for those.
  [source: /home/user/Neuron/docs/admin_api/user_admin_api.md]

### Open Synapse Admin API endpoints to replicate

All require a server-admin `access_token`. [source: /home/user/Neuron/docs/admin_api/user_admin_api.md]

**User management:**
- List: `GET /_synapse/admin/v2/users` (params `from`, `limit`, `guests`, `admins`,
  `deactivated`, `order_by`, `dir`, `name`, `user_id`, `locked`, `not_user_type`); also
  `GET /_synapse/admin/v3/users`.
- Query one: `GET /_synapse/admin/v2/users/<user_id>`.
- Create/modify: `PUT /_synapse/admin/v2/users/<user_id>`.
- Deactivate: `POST /_synapse/admin/v1/deactivate/<user_id>` (`{"erase": true}`).
- Suspend: `PUT /_synapse/admin/v1/suspend/<user_id>`.
- Reset password: `POST /_synapse/admin/v1/reset_password/<user_id>` (disabled under MSC3861).
- Get/set admin: `GET`/`PUT /_synapse/admin/v1/users/<user_id>/admin` (disabled under MSC3861).
- Shadow-ban: `POST`/`DELETE /_synapse/admin/v1/users/<user_id>/shadow_ban`.
- Rate-limit override: `GET`/`POST`/`DELETE /_synapse/admin/v1/users/<user_id>/override_ratelimit`.
- Devices: `GET`/`POST /_synapse/admin/v2/users/<user_id>/devices`,
  `GET`/`PUT`/`DELETE /_synapse/admin/v2/users/<user_id>/devices/<device_id>`, bulk
  `POST /_synapse/admin/v2/users/<user_id>/delete_devices`.
- Pushers: `GET /_synapse/admin/v1/users/<user_id>/pushers`.
- Account data: `GET /_synapse/admin/v1/users/<user_id>/accountdata`.
- Whois: `GET /_synapse/admin/v1/whois/<user_id>`.
- Login as a user: `POST /_synapse/admin/v1/users/<user_id>/login` (disabled under MSC3861).
- Memberships: `GET /_synapse/admin/v1/users/<user_id>/memberships`, `.../joined_rooms`.
- Per-user media: `GET`/`DELETE /_synapse/admin/v1/users/<user_id>/media`.
- Redact: `POST /_synapse/admin/v1/user/<user_id>/redact` (+ redact_status).
- Lookups: `GET /_synapse/admin/v1/username_available`,
  `GET /_synapse/admin/v1/auth_providers/<provider>/users/<external_id>`,
  `GET /_synapse/admin/v1/threepid/<medium>/users/<address>`.

**Room management:** list/inspect/members/state/messages/hierarchy/context (as Feature 4);
`DELETE /_synapse/admin/v2/rooms/<room_id>` (+ status), `PUT`/`GET
/_synapse/admin/v1/rooms/<room_id>/block`, `POST
/_synapse/admin/v1/rooms/<room_id_or_alias>/make_room_admin`; force-join `POST
/_synapse/admin/v1/join/<room_id_or_alias>`.

**Registration:** registration tokens `GET /_synapse/admin/v1/registration_tokens`,
`.../<token>`, `POST .../new`, `PUT .../<token>`, `DELETE .../<token>`
[source: /home/user/Neuron/docs/usage/administration/admin_api/registration_tokens.md];
shared-secret register `GET`/`POST /_synapse/admin/v1/register` (disabled under MSC3861);
account validity `POST /_synapse/admin/v1/account_validity/validity` (disabled under MSC3861).

**Reporting / dashboards / moderation:**
- Event reports: `GET /_synapse/admin/v1/event_reports`, `.../<report_id>`, `DELETE .../<report_id>`.
- Statistics: `GET /_synapse/admin/v1/statistics/users/media`,
  `GET /_synapse/admin/v1/statistics/database/rooms`.
- Media moderation: `POST /_synapse/admin/v1/media/quarantine/<server_name>/<media_id>`
  (+ `unquarantine`), `POST /_synapse/admin/v1/room/<room_id>/media/quarantine`,
  `POST /_synapse/admin/v1/media/protect/<media_id>` (+ `unprotect`),
  `POST /_synapse/admin/v1/media/delete`, `POST /_synapse/admin/v1/media/<server_name>/delete`.
- Server notices: `POST /_synapse/admin/v1/send_server_notice`.
- Purge history: `POST /_synapse/admin/v1/purge_history/<room_id>[/<event_id>]` (+ status).

### Replication notes / risks

- **Auth duality.** Support both (a) a static Synapse server-admin token (legacy/non-MAS)
  and (b) a MAS-issued admin-scoped OIDC session. Under MSC3861, route user
  create/password/admin-flag through MAS's admin API.
- **Secure-context requirement.** Serve the SPA over HTTPS.
- **Pagination quirks.** Users paginate forward only (opaque `next_token`); rooms
  paginate both ways; some `order_by` values are unindexed and DB-heavy on large servers —
  surface this in the UI.
- **Destructive-action UX.** Room deletion is largely irreversible; v2 delete/redact/purge
  are async — poll status, gate with confirmations.
- **Clean-room boundary.** The OSS ecosystem (e.g. `synapse-admin` and its fork `Ketesa`)
  proves an original equivalent is feasible against these documented open endpoints. Study
  for endpoint mapping; build the UI/state independently; do not copy element-admin's code.
- **Admin Bot tab coupling.** "Log in as Admin bot" depends on Feature 4 + the key-backup
  passphrase entered client-side; keep the supervision integration optional/pluggable.

---

## Feature 6 — Media Content Scanner

### Documented behavior / outcome

ESS Pro ships a media content scanner that sits as a proxy in front of the media
repository and scans media (typically with an AV like ClamAV) before delivery. The open
implementation documenting this exact functionality is
`element-hq/matrix-content-scanner-python` (formerly `matrix-org/matrix-content-scanner`),
"A web service for scanning media hosted by a Matrix media repository."
[source: https://github.com/element-hq/matrix-content-scanner-python]
[source: https://github.com/matrix-org/matrix-content-scanner]

Operating model:
- Runs as an HTTP proxy; all requests under the prefix `/_matrix/media_proxy/unstable/`.
- On download it retrieves the (decrypted) file from the backing media repo and scans it;
  results are cached (re-scanned only if not scanned since the scanner started).
- On a positive hit (malware) it refuses to serve the file, returning an HTTP error
  (`MCS_MEDIA_NOT_CLEAN`, served 403) instead of the bytes.
- It supports scanning **end-to-end-encrypted media**: a client posts encrypted-file
  metadata (file info + keys/IV) to the scanner, which decrypts server-side, scans, then
  serves. Uses an Olm/vodozemac server keypair; exposes its public curve25519 key so
  clients can encrypt the request body.
  [source: https://raw.githubusercontent.com/element-hq/matrix-content-scanner-python/main/README.md]

Documented proxy endpoints (under `/_matrix/media_proxy/unstable/`):
- `.../download/{serverName}/{mediaId}` — download + scan unencrypted media.
- `.../thumbnail/{serverName}/{mediaId}` — thumbnail + scan.
- `.../scan/{serverName}/{mediaId}` — scan-only verdict (unencrypted).
- `.../scan_encrypted` — POST encrypted file metadata; decrypt, scan, verdict.
- `.../public_key` — returns the scanner's current public curve25519 key.

### Config surface

Documented config keys of the open scanner:
[source: https://raw.githubusercontent.com/element-hq/matrix-content-scanner-python/main/README.md]
- `scan.script` — external scan program (where ClamAV's `clamscan`/`clamdscan` is wired).
- `scan.temp_directory` — where files are written for scanning.
- `scan.allowed_mimetypes` — restricts accepted/scanned MIME types.
- `download.base_homeserver_url` — backing homeserver/media repo to fetch from;
  `download.proxy`, `download.additional_headers`.
- `result_cache.max_size`, `result_cache.ttl`, `result_cache.exit_codes_to_ignore`.
- `crypto.pickle_key` / `crypto.pickle_path` — persisted Olm/vodozemac keypair for the
  encrypted-media flow.
- `web` — listener host/port.

On the Synapse side, hooks can enforce scanning at upload time:
- Spam-checker callback `check_media_file_for_spam(file_wrapper, file_info)` runs on
  uploaded media and can reject it.
  [source: /home/user/Neuron/docs/spam_checker.md]
  [source: /home/user/Neuron/docs/modules/spam_checker_callbacks.md]
- Media-repository callbacks (`is_user_allowed_to_upload_media_of_size`,
  `get_media_upload_limits_for_user`, `get_media_config_for_user`) gate uploads by
  size/quota. [source: /home/user/Neuron/docs/modules/media_repository_callbacks.md]

### Dependencies

- An AV/scan engine via `scan.script` — ClamAV is the documented integration.
  [source: https://docs.clamav.net/]
- Olm/vodozemac for the encrypted-media path.
- A backing media repository (stock Synapse) at `download.base_homeserver_url`.
- No Redis/PostgreSQL/workers required for the scanner itself (in-process cache).

### Open Matrix APIs / endpoints & Synapse config to replicate

The scanner proxies the standard Matrix media APIs and substitutes its
`/_matrix/media_proxy/unstable/...` paths in front of them.

**Upload (what is scanned):**
[source: https://raw.githubusercontent.com/matrix-org/matrix-spec/main/content/client-server-api/modules/content_repo.md]
- `POST /_matrix/media/v3/upload` — synchronous upload.
- `POST /_matrix/media/v1/create` (returns `content_uri`, `unused_expires_at`) then
  `PUT /_matrix/media/v3/upload/{serverName}/{mediaId}` (async flow). Too many
  unfinished creates → `429 M_LIMIT_EXCEEDED`; oversize → `413 M_TOO_LARGE`.

**Download/serve (what the proxy intercepts):**
- New authenticated endpoints (require an access token; not in query string):
  `GET /_matrix/client/v1/media/download/{serverName}/{mediaId}[/{fileName}]`,
  `GET /_matrix/client/v1/media/thumbnail/{serverName}/{mediaId}`,
  `GET /_matrix/client/v1/media/preview_url`, `GET /_matrix/client/v1/media/config`.
  `timeout_ms` (default 20000); thumbnails take `width`/`height` + `method`
  (`crop`/`scale`); v1.12 requires a `Content-Disposition` header on downloads.
- Deprecated unauthenticated endpoints: `GET /_matrix/media/v3/download/{serverName}/{mediaId}[/{fileName}]`,
  `GET /_matrix/media/v3/thumbnail/{serverName}/{mediaId}`, `/_matrix/media/v3/preview_url`,
  `/_matrix/media/v3/config`.
- Federation fetch (remote media): `GET /_matrix/federation/v1/media/download/{mediaId}`
  and `.../thumbnail/{mediaId}` — return `multipart/mixed` with two parts (JSON metadata
  + bytes or `Location` redirect), federation-signed, `timeout_ms` default 20000.

**Synapse storage/config knobs:** [source: /home/user/Neuron/docs/media_repository.md]
[source: /home/user/Neuron/docs/usage/configuration/config_documentation.md]
- `media_store_path`, `enable_local_media_storage`, `media_storage_providers` (e.g.
  `module: file_system` with `store_local`/`store_remote`/`store_synchronous`).
- `max_upload_size` (default `50M`; must match reverse-proxy `client_max_body_size`),
  `media_upload_limits`, `max_pending_media_uploads` (default 5), `unused_expiration_time`
  (default 24h).
- Upload-time gating without a separate proxy: a module implementing
  `check_media_file_for_spam` and/or `is_user_allowed_to_upload_media_of_size`.

**Replication approach (no proprietary code):** run the open `matrix-content-scanner-python`
(or a clean reimplementation) as a sidecar; route `/_matrix/media_proxy/unstable/...`
(and optionally rewrite standard download paths) through it; point
`download.base_homeserver_url` at stock Synapse's media repo; wire `scan.script` to
ClamAV. Encrypted-media support uses the open `public_key` + `scan_encrypted` endpoints +
a vodozemac/Olm keypair.

### Replication notes / risks

- `/_matrix/media_proxy/unstable/...` is an **unstable** namespace, not frozen spec —
  clients must be configured to use them (an out-of-band convention).
- The original Node `matrix-content-scanner` is deprecated; base any reimplementation on
  the maintained Python design.
- Scanning encrypted media means the scanner decrypts content server-side — a deliberate
  trust trade-off (the proxy sees plaintext) needing careful key handling.
- Stock Synapse has no built-in pre-delivery AV scan; the only in-process hook
  (`check_media_file_for_spam`) runs at **upload**, is a boolean gate, not a
  quarantine/caching scanner — so a separate proxy is needed to match the full
  download-time outcome.
- Which endpoints the proxy must intercept depends on spec/Synapse version (authenticated
  v1 media vs deprecation of unauthenticated `/_matrix/media/v3/download`).

---

## Feature 7 — Scalability / High Availability with stock Synapse

### Documented behavior / outcome

ESS Pro's HA story is Synapse deployed as multiple worker processes behind HAProxy on
Kubernetes, with horizontal/vertical autoscaling, Redis for replication/shared cache, an
external PostgreSQL, and distroless images. HAProxy load-balances and fails over across
workers; Kubernetes reschedules pods off failed nodes; autoscalers scale pod counts on
load.
[source: https://docs.element.io/latest/element-server-suite-pro/administration/guidance-on-high-availability/]
Element notes the proprietary "Synapse Pro" advantages are (a) Rust workers using
multiple CPU cores, (b) shared data caches to cut RAM, and (c) workers added/removed
elastically **without restart**, "unlike community Synapse, which has to be restarted to
pick up new workers."
[source: https://element.io/blog/scaling-to-millions-of-users-requires-synapse-pro/]

**Key clean-room finding:** the scaling **architecture** (workers + Redis replication +
sharded streams + external PostgreSQL + reverse-proxy routing) is **fully present in
stock open Synapse** and is what `matrix.org` runs. What stock Synapse cannot do without
proprietary internals is the elastic, no-restart re-sharding and the Rust
multi-core/shared-cache efficiency — those are outcomes you **approximate operationally**
(pre-provision workers, restart on shard changes), not reproduce identically.
[source: /home/user/Neuron/docs/workers.md]

Stock-Synapse worker model: [source: /home/user/Neuron/docs/workers.md]
- Processes share one PostgreSQL DB (SQLite is demo-only).
- Processes sync via a Synapse "replication" protocol over a **Redis pub/sub channel**;
  Redis also serves as a shared cache. Workers also make direct HTTP requests to each
  other for reply-needing ops.

### Config surface

Shared (cluster) config: [source: /home/user/Neuron/docs/workers.md]
[source: /home/user/Neuron/docs/usage/configuration/config_documentation.md]
- An HTTP `replication` listener on the main process (e.g. 9093); optional
  `worker_replication_secret` (replication traffic is otherwise unauthenticated — never
  expose publicly).
- `redis: { enabled: true, host, port, path, password/password_path, dbid, use_tls,
  certificate_file, private_key_file, ca_file/ca_path }` — **must** be enabled with
  workers (added 1.78.0).
- `instance_map` — maps each worker (and `main`, required if any other worker exists) to
  its replication host/port (or unix `path`); per-entry `tls: true`.
- `stream_writers` — assigns single-writer (or, for some, multi-writer) ownership of:
  `events`, `typing`, `to_device`, `account_data`, `receipts`, `presence`, `push_rules`,
  `device_lists`.
- `pusher_instances`, `federation_sender_instances` + `send_federation: false`
  (sharded by destination hash), `outbound_federation_restricted_to` (added 1.89.0 — lock
  egress to specific senders/an SBG), `run_background_tasks_on`,
  `update_user_directory_from_worker`, `notify_appservices_from_worker`,
  `media_instance_running_background_jobs`.
- Media-worker offload: `enable_media_repo: false` on main when a dedicated
  `synapse.app.media_repository` worker handles `/_matrix/media/`,
  `/_matrix/client/v1/media/`, `/_matrix/federation/v1/media/` + media admin APIs.

Per-worker config: `worker_app` (usually `synapse.app.generic_worker`), `worker_name`,
`worker_listeners` (http; plus replication if in `instance_map`/`stream_writers`).

External PostgreSQL: [source: /home/user/Neuron/docs/postgres.md]
- `database: { name: psycopg2, args: { user, password, dbname, host, cp_min, cp_max } }`;
  UTF8 / `--locale=C`; tune keepalives for remote DBs; server-side tune `shared_buffers`,
  `effective_cache_size`, `work_mem`, `maintenance_work_mem`, `autovacuum_work_mem`.

### Dependencies

- **Redis** — mandatory with workers (pub/sub replication bus + shared cache;
  `pip install matrix-synapse[redis]`).
- **PostgreSQL** — mandatory with workers (one shared instance).
- **A reverse proxy** (nginx/HAProxy/Apache/Caddy) routing endpoint patterns to workers;
  must not canonicalize URIs; `client_max_body_size` must match `max_upload_size`.
  [source: /home/user/Neuron/docs/reverse_proxy.md]
- Process supervision (systemd or Kubernetes).

### Open Matrix APIs / endpoints & Synapse config to replicate

Worker types and the endpoint patterns each owns (all open Synapse):
[source: /home/user/Neuron/docs/workers.md]
- `synapse.app.generic_worker` — universal worker. Handles `/sync`, `/events`,
  `/initialSync`; large blocks of `^/_matrix/federation/v1/...` (event/state/backfill/
  send/query/make_join/send_join, inbound `^/_matrix/federation/v1/send/`); many client
  read/write paths (`createRoom`, `publicRooms`, room state/members/messages,
  `/rooms/.*/send`, `/join|invite|leave|...`, `/profile`, `/keys/*`, login/register,
  `/search`, `/user_directory/search`). Can act as event_creator/event_persister.
- Stream-writer routing: `typing` → `.../rooms/.*/typing`; `to_device` →
  `.../sendToDevice/`; `account_data` → `.../tags` & `.../account_data`; `receipts`
  (multi-writer) → `.../rooms/.*/receipt` & `.../read_markers`; `presence` →
  `.../presence/`; `push_rules` → `.../pushrules/`; `device_lists` (multi-writer) →
  `/delete_devices`, `/devices`, `/keys/upload`, `/keys/device_signing/upload`,
  `/keys/signatures/upload`; `events` (multi-writer, **sharded by room ID**) → event
  persisters; `quarantined_media_changes` (multi-writer) →
  `^/_synapse/admin/v1/quarantine_media/.*$`.
- `synapse.app.media_repository` — owns `/_matrix/media/`, `/_matrix/client/v1/media/`,
  `/_matrix/federation/v1/media/` + media admin APIs (set `enable_media_repo: false` on
  main; one `media_instance_running_background_jobs`; multiple media workers must be
  co-located).
- `synapse.app.federation_sender`, `synapse.app.pusher` — legacy types, superseded by
  `generic_worker` + `federation_sender_instances` / `pusher_instances`; appservice/
  user_dir fold into `notify_appservices_from_worker` / `update_user_directory_from_worker`;
  background tasks via `run_background_tasks_on`.

Sharding levers to match Synapse Pro outcomes with stock Synapse:
- Shard event persistence: `stream_writers.events: [event_persister1, event_persister2]`
  (by room ID).
- Shard outbound federation: multiple `federation_sender_instances` (by destination hash).
- Multi-writer `receipts` and `device_lists`. Multiple `generic_worker`s for `/sync`
  (route per-user via consistent hashing on the access token; separate initial/incremental
  sync).
- HA fronting: HAProxy / Kubernetes Services for a stable VIP, health checks (Synapse
  exposes `/health` on every listener), pod rescheduling — the same pattern ESS Pro uses,
  achievable with stock images.

### Replication notes / risks

- **Redis is a single pub/sub bus, not yet clustered:** full Redis Cluster/Sentinel HA is
  an open request (Synapse issue #16984) — Redis can be a SPOF unless externally HA'd.
  [source: https://github.com/element-hq/synapse/issues/16984]
- **Re-sharding requires restarts:** adding/removing event persisters means you "*must*
  restart all worker instances"; `federation_sender_instances` changes require stopping
  all senders together; user-directory/appservice worker changes require a main restart.
  This is exactly the "community Synapse has to be restarted to pick up new workers" limit
  Element calls out — stock Synapse cannot match Pro's no-restart elastic scaling.
- **Single-writer streams** (most of `stream_writers`) cannot be load-balanced — only
  `events`, `receipts`, and `device_lists` are multi-writer; `appservice`/`user_dir`/
  background tasks each run on exactly one instance.
- **Replication listener security:** set `worker_replication_secret`; never expose
  replication publicly.
- **Reverse-proxy correctness:** pin `/sync` per-user; do not canonicalize URIs; keep
  `client_max_body_size` == `max_upload_size`.
- **Efficiency gap:** Pro's Rust multi-core workers + shared caches reduce CPU/RAM; stock
  (Python) Synapse needs more processes/RAM for comparable throughput, though the
  functional HA outcome (no app-process SPOF, autoscaling, failover) is reproducible.
  Distroless images are a packaging choice independently achievable for stock Synapse.

---

## Consolidated open-API index (quick reference)

| Concern | Open API surface |
| --- | --- |
| Federation transport & discovery | Server-Server API `/_matrix/federation/...`, Key API `/_matrix/key/v2/...`, `.well-known/matrix/server`, X-Matrix auth header |
| In-protocol federation ACL | `m.room.server_acl` state event |
| User lifecycle / provisioning | Synapse Admin API `/_synapse/admin/v2/users/...`, `/_synapse/admin/v1/deactivate|suspend|reset_password|...` |
| Membership & power levels | CS API `createRoom`, `/rooms/{id}/invite|join|kick|ban`, `m.room.power_levels`; Admin `make_room_admin`, `/join` |
| Spaces | `m.space`, `m.space.child`, `m.space.parent`, `/rooms/{id}/hierarchy` |
| Event ingestion (audit) | CS `/sync`, `/rooms/{id}/messages`; AS API `/_matrix/app/v1/transactions/{txnId}` |
| E2EE | CS `/keys/upload|query|claim|changes`, cross-signing, `/room_keys/...`, SSSS, to-device key sharing |
| Media | CS upload `/_matrix/media/v3/upload`, `/_matrix/media/v1/create`; authed download `/_matrix/client/v1/media/...`; federation media; spam-checker/media-repo module callbacks |
| Admin console | Full Synapse Admin API (users, rooms, media, reports, statistics, registration tokens, server notices) |
| HA / scaling | Synapse workers, `redis`, `instance_map`, `stream_writers`, reverse proxy, external PostgreSQL |
| Auth | CS `/login` (`m.login.sso`, `m.login.token`, `m.login.application_service`, `org.matrix.login.jwt`), MAS / MSC3861 delegated auth |

---

*End of Phase 1 feature analysis. Design and plan follow in `ARCHITECTURE.md`,
`FEATURE-MATRIX.md`, `PLAN.md`, and `OPEN-QUESTIONS.md`.*
