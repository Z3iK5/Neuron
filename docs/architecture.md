# Architecture

Neuron is a small monorepo of focused Python packages under `neuron/src/`. The
homeserver is the core; everything else is tooling that talks to it over standard Matrix
APIs and the Admin API.

```
┌─────────────────────────────────────────────────────────────┐
│  Matrix clients (Element, FluffyChat, …)   other homeservers  │
└───────────────┬───────────────────────────────────┬──────────┘
   Client-Server API                       Server-Server (federation)
                │                                     │
        ┌───────▼─────────────────────────────────────▼───────┐
        │                  neuron_server                       │
        │  auth · rooms (v11) · /sync · media · E2EE key relay │
        │  Client-Server API · Admin API · federation          │
        └───────▲───────────────────────┬─────────────────────┘
        Admin API │                      │ (SQLite / PostgreSQL,
                  │                      │  filesystem media store)
   ┌──────────────┴───────┐   ┌──────────▼─────────┐
   │   neuron_console     │   │  neuron_supervisor │   moderation / audit bots
   │   (web admin UI)     │   │  neuron_auditor    │   (Client-Server + Admin API)
   └──────────────────────┘   └────────────────────┘
```

## Packages

| Package | Role |
|---------|------|
| **`neuron_server`** | The Matrix homeserver. Identity & auth (registration, login, devices, access tokens), rooms with the spec's room-v11 authorization rules, `GET /sync` long-polling, a media repository, E2EE key upload/relay (the server never decrypts), mobile push notifications (pushers, rule evaluation, and Sygnal push-gateway delivery), the everyday Client-Server API, a Synapse-compatible `/_synapse/admin/...` Admin API, and server-to-server federation (signed events, key publishing/resolution, join/leave/invite, transactions, backfill, receipts, typing). |
| **`neuron_core`** | Shared library used by the console and bots: a typed homeserver Admin API client, a lightweight Client-Server client, `pydantic-settings` config, logging, error types, and the **brand** single-source-of-truth (`branding.py` — palette, type, the mark, and the server's HTML pages). |
| **`neuron_console`** | FastAPI + Jinja web admin console over the Admin API. Operator login with a signed session cookie; the admin token never reaches the browser. |
| **`neuron_supervisor`** | A privileged bot that promotes itself to room admin and moderates (kick/ban/redact). |
| **`neuron_auditor`** | A bot that streams room events to durable sinks (JSON Lines / S3), optionally decrypting E2EE rooms via `neuron_crypto`. |
| **`neuron_crypto`** | Megolm/Olm decryption helpers (libolm) for the auditor. |
| **`neuron_desktop`** | The desktop app: per-user data directory, first-run setup, a server supervisor (runs `neuron_server` as a managed child process), and a tray control. |

## Storage & state

`neuron_server` uses a single async database (SQLite for development, PostgreSQL for
production) accessed through a thin `Database` abstraction, with schema migrations
applied at startup. Uploaded media is stored on the filesystem. The Ed25519 signing key
(the server's federation identity) is stored in the database or a file you point at with
`NEURON_SERVER_SIGNING_KEY_PATH`.

## Design notes

- **One source of truth for the brand.** Colors, type, the mark geometry, and the
  server's landing/onboarding HTML all come from `neuron_core.branding`, so every
  surface (homeserver pages, console, desktop icon, repo assets) stays consistent.
- **Injectable seams for testing.** Network and process boundaries are seams the tests
  override — e.g. the federation client's `open_client` is pointed at an in-process
  second homeserver via an ASGI transport to test federation without a network, and the
  desktop supervisor takes an injectable process factory.
- **Standard APIs only.** The console and bots speak the public Client-Server API and
  the Admin API; nothing reaches into the server's internals.

For configuration and how to run each piece, see
[configuration.md](configuration.md).
