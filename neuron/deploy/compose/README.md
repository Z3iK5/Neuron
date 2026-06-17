# Local dev stack

A throwaway Synapse + PostgreSQL + Redis environment for developing and testing
Neuron services against a **real** homeserver.

> Requires Docker with the Compose plugin (`docker compose`).

## 1. Configure

```bash
cd neuron/deploy/compose
cp .env.example .env
# Edit .env: set strong-ish random values for REGISTRATION_SHARED_SECRET and
# POSTGRES_PASSWORD. For example:
#   openssl rand -hex 32   # -> REGISTRATION_SHARED_SECRET
#   openssl rand -hex 16   # -> POSTGRES_PASSWORD
```

`.env` is git-ignored; never commit it.

## 2. Start

```bash
docker compose up -d
```

On first start, the Synapse container generates its config and signing keys into
the `synapse-data` volume, then starts against PostgreSQL. Wait until it is
healthy:

```bash
docker compose ps          # STATUS should show "healthy" for synapse
curl -s http://localhost:8008/_matrix/client/versions | head -c 200
```

## 3. Create an admin user and get a token

Register a **server admin** user. Use `-k "$REGISTRATION_SHARED_SECRET"` so the
command uses the *exact* secret the running server uses (which comes from your
`.env`). Do **not** use `-c /data/homeserver.yaml` here: the active registration
secret lives in the runtime override `/data/conf.d/10-neuron-dev.yaml`, not in the
generated `homeserver.yaml`, so `-c /data/homeserver.yaml` would fail with
"HMAC incorrect".

```bash
docker compose exec synapse sh -c \
  'register_new_matrix_user -k "$REGISTRATION_SHARED_SECRET" -a -u admin -p "<your-dev-password>" http://localhost:8008'
```

Log in to obtain an access token (this token is what Neuron services use):

```bash
curl -s -XPOST http://localhost:8008/_matrix/client/v3/login \
  -d '{"type":"m.login.password","identifier":{"type":"m.id.user","user":"admin"},"password":"<your-dev-password>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])"
```

Export it for Neuron tooling and tests:

```bash
export NEURON_SYNAPSE_BASE_URL=http://localhost:8008
export NEURON_SYNAPSE_ADMIN_TOKEN=<the access_token from above>
```

## 4. Verify with neuron_core

From the `neuron/` directory, with the venv active:

```bash
python -c "import asyncio, os; from neuron_core import SynapseAdminClient; \
print(asyncio.run(SynapseAdminClient(os.environ['NEURON_SYNAPSE_BASE_URL'], os.environ['NEURON_SYNAPSE_ADMIN_TOKEN']).get_server_version()))"
```

You should see the Synapse + Python versions. The integration test
(`neuron/tests/integration/test_smoke.py`) exercises the same path and will run
automatically when `NEURON_SYNAPSE_BASE_URL` and `NEURON_SYNAPSE_ADMIN_TOKEN`
point at a reachable homeserver (otherwise it is skipped).

## 5. Stop / reset

```bash
docker compose down            # stop, keep data
docker compose down -v         # stop and DELETE all data (fresh start next time)
```
