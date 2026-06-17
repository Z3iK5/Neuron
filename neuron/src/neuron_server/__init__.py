# SPDX-License-Identifier: Apache-2.0
"""neuron_server — Neuron's own, clean-room Matrix homeserver (in progress).

Implemented **strictly from the open Matrix specification and open MSCs** —
never by reading any other homeserver's source (see ``HOMESERVER-PLAN.md`` at the
repository root). This package is the all-in-one replacement for the transitional
upstream backend: once it reaches parity (through phase HS-6) the Neuron services
point at it instead of the stock upstream image.

Phase **HS-0** (this milestone) is the foundation only:

- an ASGI application skeleton (FastAPI/Starlette);
- an async storage layer (SQLite for dev, PostgreSQL for prod) with migrations;
- the spec-discovery endpoints ``GET /_matrix/client/versions`` and
  ``GET /.well-known/matrix/client``, plus a health probe.

Identity, rooms, sync, media and E2EE arrive in later phases (HS-1..HS-6).
"""

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings

__all__ = ["create_app", "NeuronServerSettings"]

__version__ = "0.0.1"
