"""Exception types used across Neuron.

A small hierarchy:

- ``NeuronError`` — base for anything we raise on purpose.
- ``MatrixApiError`` — an HTTP+JSON error from a Matrix API (carries the HTTP
  status plus the Matrix ``errcode``/``error`` fields).
- ``SynapseAdminError`` / ``MatrixError`` — the Admin API vs Client-Server API
  flavours, so callers can tell which API failed if they care.
"""

from __future__ import annotations


class NeuronError(Exception):
    """Base class for all errors raised intentionally by Neuron code."""


class MatrixApiError(NeuronError):
    """An error response from a Matrix HTTP+JSON API.

    Matrix error bodies look like ``{"errcode": "M_...", "error": "..."}``. We
    surface the HTTP status code plus those fields so callers can react (e.g.
    treat ``404`` / ``M_NOT_FOUND`` differently from ``401`` / ``M_UNKNOWN_TOKEN``).
    """

    def __init__(
        self,
        status_code: int,
        *,
        errcode: str | None = None,
        message: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.errcode = errcode
        self.message = message
        detail = message or "<no error message>"
        code = f" {errcode}" if errcode else ""
        super().__init__(f"Matrix API error {status_code}{code}: {detail}")


class SynapseAdminError(MatrixApiError):
    """An error from the Synapse Admin API (``/_synapse/admin/...``)."""


class MatrixError(MatrixApiError):
    """An error from the Matrix Client-Server API (``/_matrix/client/...``)."""
