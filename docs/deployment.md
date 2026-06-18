# Deployment

Running Neuron for real (a private server reachable by others) means three things
beyond the local quick start: a real domain over HTTPS, a production database, and —
if you want to talk to other servers — federation reachability. Validate all of it with
`neuron-server doctor`.

## 1. Pick a server name and public URL

The server name is permanent and forms every user ID (`@you:chat.example.org`). Set it,
and the public HTTPS URL clients use, before first start:

```bash
export NEURON_SERVER_NAME=chat.example.org
export NEURON_SERVER_PUBLIC_BASE_URL=https://chat.example.org
```

## 2. Use PostgreSQL

SQLite is fine for a personal server; use PostgreSQL for anything shared:

```bash
export NEURON_SERVER_DATABASE_URL=postgresql://neuron:password@localhost/neuron
```

## 3. Terminate TLS with a reverse proxy

Run `neuron-server` bound to localhost and put a TLS-terminating reverse proxy (Caddy,
nginx, Traefik) in front of it on 443. Bind to localhost:

```bash
export NEURON_SERVER_BIND_HOST=127.0.0.1
export NEURON_SERVER_BIND_PORT=8008
```

Example with [Caddy](https://caddyserver.com) (automatic certificates):

```
chat.example.org {
    reverse_proxy 127.0.0.1:8008
}
```

The server already serves `/.well-known/matrix/client` (advertising
`NEURON_SERVER_PUBLIC_BASE_URL`) so clients can auto-discover it from the bare domain.

## 4. Lock down registration

Disable open signups and invite people with links instead:

```bash
export NEURON_SERVER_REGISTRATION_ENABLED=false
```

Create a bootstrap admin by adding their localpart to `NEURON_SERVER_ADMIN_USERS`, then
generate **invite links / QR codes** from the console's *Registration tokens* page —
they work with registration closed.

## 5. Federation (optional)

To federate with other homeservers, the server must be reachable for server-to-server
traffic. Matrix resolves a server name to its federation endpoint via, in order:

- an explicit port in the name (`chat.example.org:8448`), else
- a `/.well-known/matrix/server` delegation served at the apex domain, else
- the name on port `8448`.

If your federation endpoint differs from the apex domain, serve a delegation, e.g.:

```json
// https://chat.example.org/.well-known/matrix/server
{ "m.server": "matrix.example.org:443" }
```

**Back up the signing key.** It is your server's federation identity; losing it breaks
trust with every server that has cached it. Store it at a known path
(`NEURON_SERVER_SIGNING_KEY_PATH`) and include it in backups alongside the database.

## 6. Verify with `doctor`

```bash
neuron-server doctor          # config, database, signing key, ports, .well-known, federation
neuron-server doctor --strict # exit non-zero on warnings too (useful in CI / deploy gates)
```

`doctor` reports each check as ok / warn / fail and exits non-zero on any failure, so it
doubles as a pre-flight gate before (or after) bringing the server up.

## Persisting configuration

All settings are environment variables (see [configuration.md](configuration.md)), so
they slot naturally into a systemd unit's `Environment=` / `EnvironmentFile=`, a
container's env, or your orchestrator's secret store. Keep secrets (the admin token,
console password, database password) out of the repository.
