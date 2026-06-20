# SPDX-License-Identifier: Apache-2.0
"""neuron_core — shared building blocks for every Neuron service.

This small library exists so that the logic for talking to the homeserver,
loading configuration, and emitting structured logs is written **once** and
reused by all services (the admin console, the bots, the directory-sync service,
etc.).

Public API (the things services import):

- ``AdminClient`` — a typed client for the homeserver Admin API.
- ``MatrixClient`` — a typed client for the Client-Server API (used by bots).
- ``NeuronCoreSettings`` — base configuration loaded from environment variables.
- ``configure_logging`` / ``get_logger`` — structured logging helpers.
- ``NeuronError`` / ``MatrixApiError`` / ``AdminApiError`` / ``MatrixError`` —
  the exception types we raise.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from neuron_core.admin_client import (
    AdminClient,
    EventReportPage,
    RoomListPage,
    UserListPage,
)
from neuron_core.config import NeuronCoreSettings
from neuron_core.csapi_client import MatrixClient
from neuron_core.errors import (
    AdminApiError,
    MatrixApiError,
    MatrixError,
    NeuronError,
)
from neuron_core.logging import configure_logging, get_logger

__all__ = [
    "AdminClient",
    "MatrixClient",
    "UserListPage",
    "RoomListPage",
    "EventReportPage",
    "NeuronCoreSettings",
    "NeuronError",
    "MatrixApiError",
    "AdminApiError",
    "MatrixError",
    "configure_logging",
    "get_logger",
]

# Single source of truth: the installed package metadata (pyproject version).
try:
    __version__ = _version("neuron")
except PackageNotFoundError:  # pragma: no cover - metadata present when installed
    __version__ = "0.0.0"
