# syntax=docker/dockerfile:1
#
# Neuron homeserver image. Builds the `neuron` package with its `server` extra
# (FastAPI + uvicorn + asyncpg + Pillow + PyNaCl) into a slim, non-root runtime.
# E2EE relay needs no libolm — the homeserver passes encrypted events through, it
# does not decrypt — so the image stays lean.
#
# Build:  docker build -t neuron-server .
# Run:    see docker-compose.yml (app + Postgres) and docs/deploy.md.

# --- build stage: install into an isolated venv ---------------------------------
FROM python:3.11-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
# Only what the wheel build needs (pyproject + readme + the packages).
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[server]"

# --- runtime stage: just the venv + a non-root user ----------------------------
FROM python:3.11-slim-bookworm AS runtime
LABEL org.opencontainers.image.title="Neuron homeserver" \
      org.opencontainers.image.description="All-in-one self-hosted Matrix homeserver" \
      org.opencontainers.image.source="https://github.com/Z3iK5/Neuron"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # Bind on all interfaces inside the container; the host maps the port.
    NEURON_SERVER_BIND_HOST=0.0.0.0 \
    NEURON_SERVER_BIND_PORT=8008 \
    # Media lives on the /data volume; the signing key persists in the database
    # (leave NEURON_SERVER_SIGNING_KEY_PATH unset). Override either as needed.
    NEURON_SERVER_MEDIA_STORE_PATH=/data/media

COPY --from=builder /opt/venv /opt/venv

# Run as an unprivileged user; /data (media + anything file-backed) is a volume.
RUN useradd --system --uid 10001 --create-home neuron \
 && mkdir -p /data/media \
 && chown -R neuron:neuron /data
USER neuron
WORKDIR /data
EXPOSE 8008

# Liveness: the homeserver's /health endpoint (honours a custom bind port).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('NEURON_SERVER_BIND_PORT','8008')+'/health', timeout=5)"]

# `neuron-server serve` (default). `... doctor` runs the preflight check instead.
ENTRYPOINT ["neuron-server"]
CMD ["serve"]
