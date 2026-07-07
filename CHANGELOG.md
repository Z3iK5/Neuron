# Changelog

All notable changes to Neuron. Each release attaches desktop installers — macOS
`.dmg`, Windows `.exe`, Linux `.AppImage` — on the [Releases](https://github.com/Z3iK5/Neuron/releases) page.
Tagged releases also publish a multi-arch container image to
`ghcr.io/z3ik5/neuron-server`.

## [0.0.24] — 2026-07-07

### Added
- **Element X support.** The modern Element X mobile app now works with Neuron,
  via native sliding sync (MSC4186) — an efficient sync designed for phones that
  loads your room list in windows and streams only what changed.
- **Refresh tokens.** Clients can request refreshable, expiring access tokens
  (`/refresh`); an expired token prompts a silent refresh instead of logging you
  out. Existing sessions are unaffected.
- **Delegated login via an external identity provider (OIDC / MSC3861), optional
  and off by default.** Point Neuron at an OpenID Connect provider
  (`NEURON_SERVER_OIDC_ENABLED` + the `NEURON_SERVER_OIDC_*` settings) and it
  validates tokens against that provider instead of its own login. Left disabled,
  the built-in password and passkey login work exactly as before.

## [0.0.23] — 2026-07-07

### Added
- **Push notifications reach your phone.** The homeserver now delivers notifications
  to mobile push gateways: register a device (`/pushers`), and new messages, mentions,
  and invites are pushed to it according to your notification rules. A
  `GET /notifications` endpoint lists what has fired, and encrypted rooms send a
  count-only push (no message content leaves your server). Delivery runs in the
  background, so a slow or unreachable push gateway never delays sending a message.
  The push timeout is configurable via `NEURON_SERVER_PUSH_GATEWAY_TIMEOUT_S`.

## [0.0.22] — 2026-07-06

### Added
- **Rooms have names you can share.** Create a room with a human-readable address
  like `#general:yourserver` and join by typing it, instead of pasting an opaque
  room ID. Aliases resolve across servers too, so `#room:friendserver` works.
- **A public room directory.** Rooms created as "public" are listed in a browsable,
  searchable directory (`/publicRooms`), and you can toggle whether a room is listed
  from its settings. Other servers can query your directory over federation.

## [0.0.21] — 2026-07-05

### Added
- **Images and files from other servers now show up.** In rooms shared with people
  on other homeservers (including encrypted ones), media they send is fetched over
  federation and cached locally, so it displays like any local attachment — where
  before it appeared broken. Your server also serves your media to other servers
  the same way. A new `NEURON_SERVER_MAX_REMOTE_MEDIA_BYTES` setting (default
  100 MiB) caps how large a remote file will be fetched.

## [0.0.20] — 2026-07-05

Encryption completeness: your encrypted history now survives losing all your
devices, and encrypted chats work with people on other servers.

### Added
- **Secure Backup (encrypted message history recovery).** The server now stores
  an encrypted backup of your room keys (`/room_keys`), so a client that has lost
  every device can restore its history with your recovery key. Full backup-version
  lifecycle and per-session key storage, with the spec's "keep the better key"
  replacement rules and version rotation.
- **End-to-end encryption across servers.** Encrypted rooms now work between users
  on different homeservers: device-key lookups, one-time-key claims, and
  to-device messages (the Olm/Megolm key exchange) all travel over federation, in
  both directions, and device changes on one server notify the others. Previously
  encryption only worked between users on the same server.

### Fixed
- **Federation signature check.** A signed federation request addressed to a
  different server is no longer accepted — the destination in the signature must
  match this server.

## [0.0.19] — 2026-07-04

A full-codebase review release: every package was swept for dead and duplicated
code (~620 lines removed, behavior preserved), 11 bugs found by the review were
fixed, and the biggest missing pieces of everyday Element usage plus federation
robustness were built.

### Added
- **Account data in `/sync`.** Settings saved by your client (direct-chat list,
  favourites, and anything else stored as account data) now follow you between
  devices — delivered on initial and incremental sync, waking long-polls on change.
- **Notifications that behave like Matrix.** The server now serves the spec's
  default push rules with full editing support (`/pushrules` CRUD, per-rule
  enable/actions), and every joined room reports unread and highlight counts in
  `/sync` — so Element's room badges and notification settings work.
- **Read markers.** `/read_markers` stores your "read up to here" marker and
  private read receipts (`m.read.private`) are persisted and visible only to you.
- **More of the everyday client API:** event context (`/context`), full room
  member list (`/members`), user directory search, TURN credentials for calls
  (`/voip/turnServer`, new `NEURON_SERVER_TURN_*` settings), self-serve password
  change and account deactivation (with proper re-authentication), and
  `/rooms/{id}/forget`.
- **Federation is hub-capable.** When your server hosts a room, events arriving
  from one remote server are now relayed to the room's other servers — rooms with
  three or more servers no longer silently drop cross-server messages. Outbound
  federation traffic is chunked to the spec's 50-event transaction limit, profile
  lookups work over federation in both directions, and `.well-known` server
  delegation is now honored when dialing other servers.

### Fixed
- Unbanning now requires ban-level power (was kick-level, letting lower-powered
  moderators undo bans).
- Deleting a room with both "block" and "purge" no longer forgets the block.
- A retried federation join no longer errors; malformed to-device messages no
  longer wedge the audit bot's sync loop.
- Passkey login rejects deactivated admin accounts; login rate limiting can no
  longer be dodged by varying the identifier form; one-time keys can no longer be
  handed to two clients at once under concurrent load on PostgreSQL.
- The typing endpoint returns a proper 400 (not a 500) on a bad timeout, and the
  desktop app tolerates config files written by a newer version (downgrades no
  longer break install detection).

## [0.0.18] — 2026-06-22

### Added
- **Upgrade or fresh install.** When the desktop app starts and finds an existing
  installation from a different version, it asks whether to **upgrade** (keep all
  your data) or do a **fresh install** (erase the previous server and start over).
  A clean machine and a same-version relaunch are unaffected. Upgrade preserves the
  database, accounts, media and signing key (the server migrates itself); fresh
  removes the local data only — a note reminds you that an external PostgreSQL
  database is not dropped. A fresh install requires an explicit confirmation, and
  with no display it always upgrades (never auto-erases).

## [0.0.17] — 2026-06-21

Hardening follow-up to 0.0.16, from a code review of the new proxy / rate-limit /
media / UIA code.

### Fixed
- **Client-IP spoofing behind a wildcard-trusted proxy.** With
  `NEURON_SERVER_TRUSTED_PROXIES=*` the real client is now taken from the right-most
  `X-Forwarded-For` entry (the one the trusted proxy appended) instead of the
  left-most, which a client could forge — so per-IP rate limits and logging can't be
  spoofed. Enumerate proxies explicitly for multi-hop chains.
- **Unbounded rate-limiter memory under IP rotation.** The per-key bucket table is
  now hard-capped (LRU eviction), so rotated/spoofed client IPs can't grow it
  without bound or amplify CPU.
- **Registration-challenge flooding.** The per-IP sign-up limit is enforced at the
  interactive-auth challenge (which persists a session row), bounding abuse of the
  `uia_sessions` table; one completed sign-up still costs one token.
- **UIA sessions now honour their TTL on read**, not only via the background sweep.
- **Console media bulk-delete reports failures** (count + logs) instead of silently
  reporting "Deleted 0" when the blob/object store is unavailable.
- **Console search treats `%` and `_` literally** (media-by-uploader and
  users-by-name), instead of as SQL `LIKE` wildcards.
- **The `/get-started` rate-limit response renders the normal page** instead of a
  raw JSON error.

### Changed
- The Caddy compose stack now **requires** `NEURON_SERVER_CONSOLE_SESSION_SECRET`
  (fails fast if unset) rather than silently using a per-restart random secret that
  would log admins out on every restart.
- The server logs a startup note when request rate limiting is on but no trusted
  proxies are configured (per-IP limits assume a directly-exposed server).

## [0.0.16] — 2026-06-21

The multi-worker scaling and deployment release.

### Added
- **Run safely with a worker pool / multiple processes on PostgreSQL.** Stream IDs
  are allocated from database sequences; a multi-writer "persisted-upto" position
  tracker plus an idle-instance heartbeat means no events are lost or skipped with
  more than one writer; `/sync` long-polls wake across workers via Postgres
  LISTEN/NOTIFY (typing is now database-backed too); concurrent worker startup is
  serialized with an advisory lock; and inbound federation transactions are
  de-duplicated while the send outbox is drained by a single owner so a second
  worker never double-sends.
- **PostgreSQL backend** for real: a proper connection pool and a BIGINT schema.
- **Deployment artifacts.** A `Dockerfile` + `docker-compose.yml` (app on
  Postgres), a ready-to-run `docker-compose.caddy.yml` stack with automatic HTTPS,
  and a CI workflow that publishes a multi-arch (amd64 + arm64) image to GHCR on
  each version tag.
- **Trusted reverse-proxy support.** Behind a configured proxy, Neuron uses the
  real client IP and scheme from `X-Forwarded-*` (`NEURON_SERVER_TRUSTED_PROXIES`),
  and the admin-console session cookie can be marked `Secure`
  (`NEURON_SERVER_SESSION_HTTPS_ONLY`).
- **S3 (object-storage) media backend** so multiple hosts can share uploaded media.
- **Database-backed UIA sessions** so an in-progress registration can be completed
  by any worker (no sticky load balancer required).
- **Request rate limiting** on abuse-prone endpoints — per account/sender and per
  client IP (login brute-force, sign-up spam) — returning `M_LIMIT_EXCEEDED`.
- **Optional Prometheus `/metrics` endpoint** (`NEURON_SERVER_METRICS_ENABLED`).
- **Deeper admin console**, Synapse-Admin style: user devices/sessions with
  force-logout and joined-rooms; a room members table (force-leave, make-admin)
  with a state viewer; registration-token expiry and custom tokens; bulk-dismiss
  for reports; an editable runtime-settings page; and a **Media** page that lists
  and purges uploads.
- **Federation state-resolution v2** wired onto the live path behind a default-off
  flag (`NEURON_SERVER_STATE_RES_V2`).

### Changed
- **Desktop first-run lets you choose the database backend** — SQLite for personal
  use, or a PostgreSQL URL for a medium/large deployment.

## [0.0.15] — 2026-06-20

### Added
- **Moderation report triage**: a report detail page with the reported event in
  context, a dismiss action, and a paginated report list.
- **Bulk moderation**: multi-select shadow-ban / deactivate on Users, and block /
  delete on Rooms.

## [0.0.14] — 2026-06-20

### Changed
- **Admin console restyle.** A light/dark theme driven by CSS variables with a
  topbar toggle, and a left side-nav shell with a responsive drawer.

## [0.0.13] — 2026-06-20

### Changed
- **Shadow-ban now covers state events, redactions and invites** (not just
  messages), including the room-creation invite-list path; membership actions stay
  un-gated so the ban remains undetectable.
- **Version is single-sourced** from the installed package metadata — no more
  hardcoded version literals across the server, desktop and federation surfaces.

## [0.0.12] — 2026-06-20

### Added
- **Moderation propagates over federation.** Kicks, bans, leaves, invites and
  redactions on rooms this server hosts are now sent to remote members' servers,
  with an authority check on inbound redactions.

## [0.0.11] — 2026-06-20

### Added
- **Passkey (WebAuthn) sign-in for the merged admin console**, scoped per admin
  account.

## [0.0.10] — 2026-06-20

### Added
- **First-run wizard** that flows from settings into getting-started.

### Changed
- The console login page redirects to `/get-started` on a brand-new server (no
  account yet) instead of a dead-end login.

## [0.0.9] — 2026-06-19

### Added
- **Real moderation tools**: room block, shadow-ban, delete/purge, message
  redaction, abuse reports, and server notices.

## [0.0.8] — 2026-06-19

### Fixed
- Corrected a console settings environment-variable name so the desktop app's
  settings reach the server process.

## [0.0.7] — 2026-06-19

### Added
- **Desktop server-settings window**, a native pre-start window, and an in-console
  **doctor** health check.

## [0.0.6] — 2026-06-19

### Fixed
- First-account admin detection, the displayed server version, and overview /
  welcome-page polish.

## [0.0.5] — 2026-06-19

### Changed
- **The admin console is now served by the homeserver itself** (merged into
  `neuron_server`) instead of running as a separate app.

## [0.0.4] — 2026-06-19

### Fixed
- Cross-platform desktop **first-run crashes** introduced in 0.0.3.

### Changed
- Release notes are generated once per release instead of once per build job.

## [0.0.3] — 2026-06-18

### Added
- **Windows MSIX package** for Microsoft Store submission, built every release and
  uploaded as a workflow artifact (not attached to the GitHub Release). Self-signed
  for sideload testing by default; set the `MSIX_*` repo variables (Partner Center
  identity) to make it Store-ready.
- **macOS installers are signed & notarized** (Developer ID) when the Apple signing
  secrets are configured in CI, so they launch without a Gatekeeper warning. Builds
  stay unsigned (and CI green) when the secrets are absent.
- **Passkey (WebAuthn) login for the admin console.** Enrol a passkey (Touch ID /
  Windows Hello / a security key) from the new **Passkeys** page and sign in with it
  instead of the console password. Credentials are kept in a small file under
  `NEURON_CONSOLE_DATA_DIR`; relying-party id/origin auto-derive from the request
  (override with `NEURON_WEBAUTHN_RP_ID` / `NEURON_WEBAUTHN_ORIGIN` behind a proxy).

### Changed
- **Desktop first run now lets you set your own password.** Instead of creating a
  default admin with a generated password, the app opens the browser to the in-app
  sign-up and makes the **first account you create the server administrator** (new
  `NEURON_SERVER_FIRST_USER_ADMIN` setting). `WELCOME.txt` points you at the sign-up
  link — no default password to change.

## [0.0.2] — 2026-06-18

### Fixed
- **Desktop app first run no longer crashes** with `input(): lost sys.stdin`. A
  double-clicked GUI app has no console, so first-run setup now runs
  non-interactively: it creates your admin account automatically and records the
  credentials in `WELCOME.txt` (which the app opens for you), instead of prompting
  on a terminal that isn't there.
- **Desktop app falls back to running the server** in the foreground if the
  tray/menu-bar backend can't start, instead of quitting silently.

## [0.0.1] — 2026-06-18

### Added
- **Matrix homeserver** (`neuron_server`): identity & auth, rooms (room v11),
  `GET /sync`, a media repository, E2EE key relay, the Client-Server API, a
  Synapse-compatible Admin API, and server-to-server federation.
- **Admin console** with shareable registration **invite links + QR codes**.
- **In-browser onboarding** (`/get-started`) and a **`neuron-server doctor`**
  preflight / health command.
- **Desktop app** with native installers for macOS, Windows, and Linux.
