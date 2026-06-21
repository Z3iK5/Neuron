# Deploying Neuron with Docker + PostgreSQL

For personal / small use, the [desktop app](desktop.md) (bundled SQLite) is the
easiest path. For a medium/large deployment, run the homeserver on PostgreSQL with
the Docker stack here.

## Quick start

```bash
cd neuron
cp .env.example .env          # then edit NEURON_SERVER_NAME and the secrets
docker compose up -d --build
```

This brings up two services:

- **`db`** — `postgres:16` (data in the `neuron-pgdata` volume).
- **`neuron`** — the homeserver, on `http://localhost:8008` (one process, a sized
  connection pool). It waits for Postgres to be healthy, runs migrations, then serves.

Finish setup in the browser at **`/get-started`** — the first account you create
becomes the server admin (`NEURON_SERVER_FIRST_USER_ADMIN=true`). Manage the server
at **`/console`**.

### Prebuilt image

Tagged releases publish a multi-arch (amd64 + arm64) image to GHCR, so you don't
have to build from source:

```bash
docker pull ghcr.io/z3ik5/neuron-server:latest   # or pin a version, e.g. :0.0.16
```

The bundled `docker-compose.yml` builds from source; the TLS stack below
(`docker-compose.caddy.yml`) pulls this image by default.

## Configuration

All settings are `NEURON_SERVER_*` environment variables (see `.env.example`). The
load-bearing ones:

| Variable | Purpose |
|---|---|
| `NEURON_SERVER_NAME` | Permanent server identity (the domain in every Matrix ID). **Set before first start — it cannot change.** |
| `NEURON_SERVER_PUBLIC_BASE_URL` | Public URL clients use (advertised via `.well-known`). Use your real `https://` URL. |
| `POSTGRES_PASSWORD` | Postgres password (used by both services). Change it. |
| `NEURON_SERVER_DB_POOL_SIZE` | App connection-pool size — safe to raise above 1. |
| `NEURON_SERVER_CONSOLE_SESSION_SECRET` | Stable secret so console sessions survive restarts (`openssl rand -hex 32`). |
| `NEURON_SERVER_TRUSTED_PROXIES` | Proxy IP(s) to trust for `X-Forwarded-*` (or `*`). Set this when behind a reverse proxy. |
| `NEURON_SERVER_SESSION_HTTPS_ONLY` | Mark the console session cookie `Secure`. Set `true` in production (HTTPS). |

The federation **signing key** persists in the database (no key file to manage);
back up Postgres and you've backed up the server's identity.

## Behind a reverse proxy (TLS)

In production, terminate TLS at a reverse proxy (Caddy/nginx/Traefik) in front of
Neuron and:

1. Set `NEURON_SERVER_PUBLIC_BASE_URL` to your real `https://` URL.
2. Set `NEURON_SERVER_SESSION_HTTPS_ONLY=true` so the admin-console session cookie
   is only sent over HTTPS.
3. Set `NEURON_SERVER_TRUSTED_PROXIES` to the proxy's address so Neuron uses the
   real client IP from `X-Forwarded-For` (and the original scheme from
   `X-Forwarded-Proto`) instead of the proxy's. List the proxy hop(s) explicitly,
   e.g. `NEURON_SERVER_TRUSTED_PROXIES=172.18.0.2`; use `*` only when Neuron is
   reachable *solely* through the proxy (e.g. bound to localhost or a private
   Docker network). **Leave it empty for a directly-exposed server** — otherwise a
   client could spoof its IP via a forged header. Setting this is also what makes
   the per-IP rate limits (login spray + sign-up spam) effective behind a proxy —
   without it every request shares the proxy's single bucket.

   Neuron resolves the client as the right-most `X-Forwarded-For` entry that isn't
   one of your trusted proxies, so addresses an attacker prepends are ignored. Make
   sure the proxy is configured to *append* (not replace) `X-Forwarded-For`.

The proxy must forward both headers. Minimal Caddy example:

```caddyfile
matrix.example.org {
    reverse_proxy neuron:8008
}
```

Caddy sets `X-Forwarded-For` and `X-Forwarded-Proto` automatically; with the
container on the same Docker network, set `NEURON_SERVER_TRUSTED_PROXIES` to the
proxy container's IP (or `*` if Neuron isn't otherwise reachable).

### Ready-made TLS stack (Caddy)

`docker-compose.caddy.yml` runs the whole thing with automatic HTTPS — Caddy on
443 in front of Neuron (not published to the host) and Postgres:

```bash
cd neuron
cp .env.example .env                 # set your domain + secrets (https:// URL)
$EDITOR Caddyfile                    # replace matrix.example.org with your domain
docker compose -f docker-compose.caddy.yml up -d
```

Caddy obtains a Let's Encrypt certificate on first request (DNS must already point
at the host, ports 80 + 443 open). Because Neuron is reachable only through Caddy,
the compose file sets `NEURON_SERVER_TRUSTED_PROXIES=*` and
`NEURON_SERVER_SESSION_HTTPS_ONLY=true` for you.

## Health & diagnostics

- The container's healthcheck polls `/health`.
- Run the built-in preflight any time:
  ```bash
  docker compose run --rm neuron doctor          # config + DB + key + media + network
  docker compose run --rm neuron doctor --offline  # skip network checks
  ```

## Metrics (Prometheus)

Set `NEURON_SERVER_METRICS_ENABLED=true` to expose a Prometheus `/metrics` endpoint
(HTTP request counts + latency by method/route/status, plus process metrics). It
needs the `metrics` extra (`prometheus-client`), which the base image doesn't
include — build with it (`pip install ".[server,metrics]"`) or add it in a derived
image. **Restrict `/metrics` at the proxy/network level** — it's meant for your
Prometheus scraper, not the public internet.

## Backups

- **Database** (includes the signing key + all state):
  ```bash
  docker compose exec db pg_dump -U neuron neuron > neuron-backup.sql
  ```
- **Media**: the `neuron-data` volume (`/data/media`).

## Scaling beyond one process

The default stack is **one app process** with a connection pool — the right shape
for most medium deployments and fully safe (the multi-writer position tracker keeps
`/sync` correct under pool concurrency).

Running **multiple app processes** is also correctness-safe (cross-worker `/sync`
wakeups via Postgres `LISTEN/NOTIFY`, contiguous stream positions with a per-instance
heartbeat). Before scaling out, note:

- Give each process a **distinct** `NEURON_SERVER_INSTANCE_NAME` (stable across
  restarts) and put them behind a load balancer.
- Use **S3 media** (below) so workers don't depend on a shared local disk — this is
  what unlocks **cross-host** scale-out. (With filesystem media, multiple processes
  must share the same `/data` volume, i.e. run on the same host.)
- A single shared Postgres serves them all; size `NEURON_SERVER_DB_POOL_SIZE` per
  process for your connection budget.

## Media on S3 (object storage)

By default media blobs are stored on the local filesystem (`/data/media`). For
multi-host deployments, store them in an S3-compatible bucket instead so every
worker reads/writes the same media:

```env
NEURON_SERVER_MEDIA_BACKEND=s3
NEURON_SERVER_S3_MEDIA_BUCKET=my-neuron-media
# Optional — for S3-compatible stores (MinIO, Cloudflare R2, ...); omit for AWS S3:
NEURON_SERVER_S3_MEDIA_ENDPOINT_URL=https://minio.example:9000
NEURON_SERVER_S3_MEDIA_REGION=us-east-1
NEURON_SERVER_S3_MEDIA_PREFIX=media/
```

- **Credentials** come from the standard AWS chain — `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` env vars, or an instance/role profile — never from Neuron
  config, so secrets stay out of the config file.
- S3 media needs the `boto3` dependency, which is **not** in the base server image.
  Install the `s3` extra (`pip install ".[server,s3]"`) — e.g. build the image with
  it, or add it in a derived image. The default filesystem path needs nothing extra.
- The metadata (`media` table) still lives in Postgres; only the blobs move to S3.
