"""neuron_core — shared building blocks for every Neuron service.

This small library exists so that the logic for talking to Synapse, loading
configuration, and emitting structured logs is written **once** and reused by
all services (the admin console, the bots, the directory-sync service, etc.).

Public API (the things services import):

- ``SynapseAdminClient`` — a typed client for the open Synapse Admin API.
- ``MatrixClient`` — a typed client for the Client-Server API (used by bots).
- ``NeuronCoreSettings`` — base configuration loaded from environment variables.
- ``configure_logging`` / ``get_logger`` — structured logging helpers.
- ``NeuronError`` / ``MatrixApiError`` / ``SynapseAdminError`` / ``MatrixError`` —
  the exception types we raise.
"""

from neuron_core.admin_client import (
    EventReportPage,
    RoomListPage,
    SynapseAdminClient,
    UserListPage,
)
from neuron_core.config import NeuronCoreSettings
from neuron_core.csapi_client import MatrixClient
from neuron_core.errors import (
    MatrixApiError,
    MatrixError,
    NeuronError,
    SynapseAdminError,
)
from neuron_core.logging import configure_logging, get_logger

__all__ = [
    "SynapseAdminClient",
    "MatrixClient",
    "UserListPage",
    "RoomListPage",
    "EventReportPage",
    "NeuronCoreSettings",
    "NeuronError",
    "MatrixApiError",
    "SynapseAdminError",
    "MatrixError",
    "configure_logging",
    "get_logger",
]

__version__ = "0.0.1"
