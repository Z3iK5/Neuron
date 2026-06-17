# SPDX-License-Identifier: Apache-2.0
"""Matrix Client-Server API error responses.

The spec defines a standard error body: ``{"errcode": "M_...", "error": "..."}``
returned with an appropriate HTTP status code. We raise :class:`MatrixError` from
endpoints and a single exception handler turns it into that JSON body, so every
error the server emits has the spec-mandated shape.
"""

from __future__ import annotations

from starlette.responses import JSONResponse


class MatrixError(Exception):
    """A Client-Server API error to return to the client.

    :param status_code: the HTTP status code (e.g. 404, 401, 403).
    :param errcode: the Matrix error code (e.g. ``M_UNRECOGNIZED``).
    :param error: a human-readable description.
    """

    def __init__(self, status_code: int, errcode: str, error: str) -> None:
        self.status_code = status_code
        self.errcode = errcode
        self.error = error
        super().__init__(f"{errcode}: {error}")

    def to_response(self) -> JSONResponse:
        """Render this error as the spec's JSON error body."""
        return JSONResponse(
            status_code=self.status_code,
            content={"errcode": self.errcode, "error": self.error},
        )


def unrecognized(error: str = "Unrecognized request") -> MatrixError:
    """The error for an unknown endpoint/method under ``/_matrix`` (404 M_UNRECOGNIZED)."""
    return MatrixError(404, "M_UNRECOGNIZED", error)
