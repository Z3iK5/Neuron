# Configuration & running each component

Every Neuron service is configured through **environment variables** prefixed with
`NEURON_`. Secrets (tokens, passwords) are read from the environment — never commit
them. For local development you can put them in a git-ignored `.env` file next to where
you run the command.

The authoritative list of options for each service lives in its `config.py`
(`neuron/src/<package>/config.py`); the tables below cover the ones you'll actually set.

---

## Homeserver — `neuron_server`

```bash
cd neuron
pip install -e ".[server]"

export NEURON_SERVER_NAME=neuron.local
export NEURON_SERVER_PUBLIC_BASE_URL=http://localhost:8008
export NEURON_SERVER_DATABASE_URL=sqlite:///./neuron_server.db   # or postgresql://user:pass@host/db
neuron-server                 # serves on 127.0.0.1:8008
neuron-server doctor          # preflight / health check (add --offline to skip network)
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `NEURON_SERVER_NAME` | `neuron.local` | The server's permanent name — the domain part of every `@user:server` ID. Must not change after the database is created. |
| `NEURON_SERVER_PUBLIC_BASE_URL` | `http://localhost:8008` | Public Client-Server API URL; advertised via `/.well-known/matrix/client`. |
| `NEURON_SERVER_DATABASE_URL` | `sqlite:///./neuron_server.db` | `sqlite:///…` for dev, `postgresql://…` for production. |
| `NEURON_SERVER_REGISTRATION_ENABLED` | `true` | Open registration. **Set `false` in production** and hand out invite links. |
| `NEURON_SERVER_ADMIN_USERS` | _(empty)_ | Comma-separated localparts/IDs always treated as server admins (e.g. `admin,ops`). Needed to use the console / Admin API. |
| `NEURON_SERVER_MEDIA_STORE_PATH` | `./neuron-media` | Directory for uploaded media. |
| `NEURON_SERVER_MAX_UPLOAD_BYTES` | `52428800` (50 MiB) | Maximum upload size. |
| `NEURON_SERVER_SIGNING_KEY_PATH` | _(empty → DB)_ | Path to the Ed25519 federation signing key; if empty it is generated and stored in the database. **Back this up** — it is the server's federation identity. |
| `NEURON_SERVER_BIND_HOST` / `NEURON_SERVER_BIND_PORT` | `127.0.0.1` / `8008` | Where the ASGI server binds. |
| `NEURON_SERVER_FEDERATION_RETRY_INTERVAL_S` | `30` | How often queued outbound federation transactions are retried. |
| `NEURON_SERVER_TURN_URIS` | `[]` | TURN server URIs advertised via `/voip/turnServer` so calls work across NATs, as a JSON list — e.g. `'["turn:turn.example.org:3478?transport=udp"]'`. Empty = no TURN. |
| `NEURON_SERVER_TURN_SHARED_SECRET` | _(unset)_ | Must match coturn's `static-auth-secret`; used to mint time-limited TURN credentials (REST scheme). Required for TURN to be advertised. |
| `NEURON_SERVER_TURN_TTL_S` | `86400` (24 h) | Lifetime of issued TURN credentials, in seconds. |
| `NEURON_SERVER_LOG_LEVEL` / `NEURON_SERVER_LOG_FORMAT` | `INFO` / `json` | Logging level and format (`json` or `console`). |

**Register the first user** (open registration, UIA `m.login.dummy`):

```bash
S=$(curl -s -XPOST localhost:8008/_matrix/client/v3/register \
     -d '{"username":"alice","password":"choose-a-password"}' | jq -r .session)
curl -s -XPOST localhost:8008/_matrix/client/v3/register \
  -d "{\"username\":\"alice\",\"password\":\"choose-a-password\",\"auth\":{\"type\":\"m.login.dummy\",\"session\":\"$S\"}}"
```

Or just open the homeserver in a browser and use **Get started**. To make a user an
admin, add their localpart to `NEURON_SERVER_ADMIN_USERS` and restart.

---

## Admin console — `neuron_console`

A web UI over the homeserver Admin API. The admin token stays server-side and is never
sent to the browser.

```bash
cd neuron
pip install -e ".[console]"

export NEURON_HOMESERVER_URL=http://localhost:8008
export NEURON_HOMESERVER_ADMIN_TOKEN=<a server-admin access token>
export NEURON_SERVER_NAME=neuron.local          # to create users by username
export NEURON_CONSOLE_PASSWORD=<a password to log in to the console>
uvicorn neuron_console.app:app --port 8080      # open http://localhost:8080
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `NEURON_HOMESERVER_URL` | `http://localhost:8008` | Homeserver the console talks to (Admin API). |
| `NEURON_HOMESERVER_ADMIN_TOKEN` | _(required)_ | A server-admin access token. |
| `NEURON_HOMESERVER_PUBLIC_URL` | _(falls back to `…_URL`)_ | Public homeserver address used to build **invite links / QR codes** — set this if the console reaches the server over a private network. |
| `NEURON_CONSOLE_PASSWORD` | _(required)_ | Operator login password. |
| `NEURON_CONSOLE_SESSION_SECRET` | _(random)_ | Signs the session cookie; set a stable value in production so sessions survive restarts. |
| `NEURON_SERVER_NAME` | _(empty)_ | Server name, used to build `@user:server` IDs from a localpart. |
| `NEURON_AUTH_MODE` | `classic` | `mas` if the homeserver delegates auth to a Matrix Authentication Service (disables some admin actions). |
| `NEURON_CONSOLE_DATA_DIR` | `~/.neuron-console` | Where the console keeps small state (the registered-passkeys file). |
| `NEURON_WEBAUTHN_RP_ID` / `NEURON_WEBAUTHN_ORIGIN` | _(from request)_ | WebAuthn relying-party id + origin for passkeys; set these when behind a reverse proxy so they match the browser address (e.g. `chat.example.org` / `https://chat.example.org`). |

The console supports browsing **and** writes (create/modify/deactivate users, reset
passwords, shadow-ban, registration tokens, server notices, room block/delete,
redaction), with CSRF protection and confirmation prompts. The **Registration tokens &
invite links** page generates a shareable signup link and QR for each token — these let
people self-register even when open registration is disabled.

**Passkeys.** The **Passkeys** page lets an operator enrol a passkey (Touch ID / Windows
Hello / a security key) and then sign in with it from the login page instead of the
console password. Passkeys need a secure context — `localhost` or HTTPS. Credentials are
stored under `NEURON_CONSOLE_DATA_DIR`.

---

## Supervision bot — `neuron_supervisor`

A privileged bot that keeps itself promoted to room admin (via the Admin API's
`make_room_admin`) so any room can be moderated, and performs kick/ban as the bot.

```bash
export NEURON_HOMESERVER_URL=http://localhost:8008
export NEURON_HOMESERVER_ADMIN_TOKEN=<server-admin token>
export NEURON_SUPERVISOR_BOT_USER_ID=@supervisor:neuron.local
export NEURON_SUPERVISOR_BOT_TOKEN=<the bot account's access token>

python -m neuron_supervisor sync    # promote the bot in all rooms once
python -m neuron_supervisor run     # keep re-promoting on a timer
```

With the same two `SUPERVISOR_BOT_*` values set for the console, its **Supervision** tab
can promote the bot and adds **Kick/Ban** buttons to each room's member list.

---

## Audit bot — `neuron_auditor`

Joins rooms and streams every event to a durable sink (JSON Lines and/or S3). A resume
token is persisted, so restarts continue without gaps or duplicates.

```bash
export NEURON_HOMESERVER_URL=http://localhost:8008
export NEURON_AUDITOR_BOT_TOKEN=<the audit bot account's access token>
export NEURON_AUDITOR_SINK=file                 # file | s3 | both
export NEURON_AUDITOR_FILE_PATH=audit-log.jsonl
python -m neuron_auditor run
```

For S3/MinIO set `NEURON_AUDITOR_SINK=s3` (or `both`) and the `NEURON_AUDITOR_S3_*`
variables (see `src/neuron_auditor/config.py`).

**Encrypted rooms.** Install the E2EE extra (`pip install -e ".[e2e]"`, needs system
`libolm`). In automatic mode the bot gets a persistent Olm device, publishes its keys,
and ingests room keys sent to it:

```bash
export NEURON_AUDITOR_E2E_DEVICE_STORE=/path/to/auditor-device.json
export NEURON_AUDITOR_E2E_CROSS_SIGNING=true     # publish a cross-signed identity (optional)
```

Or decrypt from a key file with `NEURON_AUDITOR_E2E_KEY_FILE=/path/to/room-keys.json`.

> **Limits.** Decryption is forward-only (messages sent before the bot held the key
> can't be read), and a sending client must still choose to share keys with the bot —
> typically after verifying its device. The device store, session store, key file, and
> the (plaintext) audit log can all expose message content — protect them like secrets.

---

## Desktop app

The desktop app stores everything in a per-user data directory and is configured by its
first-run wizard rather than environment variables. The one override:

| Variable | Purpose |
|----------|---------|
| `NEURON_DATA_DIR` | Override the data directory (database, media, signing key, `config.json`). |

Default locations: macOS `~/Library/Application Support/Neuron`, Windows
`%LOCALAPPDATA%\Neuron`, Linux `~/.local/share/Neuron`. See **[desktop.md](desktop.md)**.
