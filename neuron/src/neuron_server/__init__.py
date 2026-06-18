# SPDX-License-Identifier: Apache-2.0
"""neuron_server — Neuron's Matrix homeserver, built on the open Matrix spec.

An ASGI application (FastAPI/Starlette) over an async storage layer (SQLite for
dev, PostgreSQL for prod) with migrations. It implements identity & auth, rooms
(room v11), ``GET /sync``, a media repository, E2EE key relay, the Client-Server
API, a Synapse-compatible Admin API, and server-to-server federation.

See the repository docs (``docs/architecture.md``, ``docs/configuration.md``) for
the full picture and how to run it.
"""

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

__all__ = ["create_app", "NeuronServerSettings"]

__version__ = "0.0.1"
