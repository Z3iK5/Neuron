<div align="center">

<img src="neuron/assets/brand/neuron-icon.png" alt="NEURON" width="120" height="120">

# NEURON

**matrix homeserver**

_Your private chat, on your own server. Self-hosted Matrix, end-to-end encrypted._

<img src="neuron/assets/brand/neuron-social.png" alt="NEURON — matrix homeserver" width="720">

[![CI](https://github.com/Z3iK5/Neuron/actions/workflows/neuron-ci.yml/badge.svg)](https://github.com/Z3iK5/Neuron/actions/workflows/neuron-ci.yml)
&nbsp;[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-1C3D5F.svg)](LICENSE)

</div>

---

**Neuron** is an all-in-one, self-hosted [Matrix](https://matrix.org) platform: your
**own** homeserver plus the tooling to run it — a web admin console, moderation and
audit bots, and a one-click desktop app. Run a private chat server for yourself, your
family, or your team, on hardware you control.

- 🏠 **Your server, your data.** Accounts, rooms, and messages live on your machine.
- 🔒 **End-to-end encrypted.** Standard Matrix E2EE; the server stores and relays keys, it never reads your messages.
- 🌐 **Talks to the whole Matrix network.** Works with [Element](https://element.io), FluffyChat, and any Matrix client; federates with other servers.
- 🖥️ **Runs anywhere.** A `pip`-installable server, or a desktop app with native installers for macOS, Windows, and Linux.

## What's inside

| Component | What it is |
|-----------|------------|
| **`neuron_server`** | The Matrix homeserver: identity & auth, rooms (room v11 authorization rules), `GET /sync`, a media repository, E2EE key relay, the everyday Client-Server API, a Synapse-compatible **Admin API**, and **server-to-server federation** (signed events, key resolution, join/leave/invite, live messaging, backfill, receipts, typing). |
| **`neuron_console`** | A branded web admin console: browse/manage users & rooms, registration tokens with **shareable invite links + QR**, content reports, and moderation. |
| **Neuron Desktop** | Run your own homeserver as an installed app — a first-run setup wizard, a menu-bar/tray control, and native installers. |
| **`neuron_supervisor` / `neuron_auditor`** | Optional moderation and audit bots. |

## Install & run

You need **Python 3.11+**. The server defaults to a local SQLite file — no database to set up.

```bash
cd neuron
pip install -e ".[server]"

export NEURON_SERVER_NAME=neuron.local
export NEURON_SERVER_PUBLIC_BASE_URL=http://localhost:8008
neuron-server                 # serves on http://localhost:8008
neuron-server doctor          # preflight: config, database, keys, reachability
```

### First run — create your account in the browser

Open **http://localhost:8008** and click **Get started** to create an account right in
the browser, then point any Matrix client (Element, FluffyChat, …) at your homeserver
address and sign in with your new `@you:neuron.local` ID. No client needed to bootstrap.

Prefer to invite others? In the [admin console](docs/configuration.md#admin-console)
create a **registration token** and share its **invite link or QR code** — they work
even with open registration disabled.

### Prefer an app?

```bash
pipx install "neuron[desktop]"   # or: pip install -e ".[desktop]"
neuron-desktop                   # first-run wizard, then starts the server
```

The wizard picks a server name, creates your admin account, and keeps all state in a
per-user data directory. Native installers (`.dmg` / `.exe` / `.AppImage`) are built per
release — see **[docs/desktop.md](docs/desktop.md)**.

## Documentation

- **[Installation & running each component](docs/configuration.md)** — server, console, bots, and the full `NEURON_*` configuration reference
- **[Desktop app & installers](docs/desktop.md)** — first-run, data directory, building the native installers
- **[Deployment](docs/deployment.md)** — production: reverse proxy + TLS, `.well-known`, PostgreSQL, federation
- **[Architecture](docs/architecture.md)** — how the components fit together
- **[Brand assets](neuron/assets/brand/)** — the NEURON mark, palette, and type
- **[Contributing](CONTRIBUTING.md)** — dev setup and the checks CI runs

## Status

- ✅ **Homeserver MVP** — identity/auth, rooms, sync, media, E2EE key relay, Client-Server + Admin APIs.
- ✅ **Federation** — functional end-to-end across two servers (signed events, joins/leaves/invites, messaging, backfill, receipts, typing).
- ✅ **Desktop installers** — macOS `.dmg`, Windows `.exe`, Linux `.AppImage` build in CI.
- 🔜 **Next** — code signing / notarization, federation conformance hardening, presence.

Some Admin endpoints (shadow-ban, server notices, async purge/redaction jobs, content
reports) are spec-shaped stubs, and registration defaults to **open** — set
`NEURON_SERVER_REGISTRATION_ENABLED=false` (and hand out invite links) for a private
server.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
