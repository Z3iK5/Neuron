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

    def __init__(
        self,
        status_code: int,
        errcode: str,
        error: str,
        *,
        extra: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.errcode = errcode
        self.error = error
        # Extra spec-defined body fields (e.g. ``retry_after_ms``) and response
        # headers (e.g. ``Retry-After``), merged into the rendered response.
        self.extra = extra or {}
        self.headers = headers or {}
        super().__init__(f"{errcode}: {error}")

    def to_response(self) -> JSONResponse:
        """Render this error as the spec's JSON error body."""
        return JSONResponse(
            status_code=self.status_code,
            content={"errcode": self.errcode, "error": self.error, **self.extra},
            headers=self.headers or None,
        )


def unrecognized(error: str = "Unrecognized request") -> MatrixError:
    """The error for an unknown endpoint/method under ``/_matrix`` (404 M_UNRECOGNIZED)."""
    return MatrixError(404, "M_UNRECOGNIZED", error)


def limit_exceeded(retry_after_ms: int) -> MatrixError:
    """The rate-limit error (429 ``M_LIMIT_EXCEEDED``) with a retry hint.

    Carries the spec's ``retry_after_ms`` body field and a ``Retry-After`` header
    (seconds, rounded up) so clients back off.
    """
    return MatrixError(
        429,
        "M_LIMIT_EXCEEDED",
        "Too many requests",
        extra={"retry_after_ms": retry_after_ms},
        headers={"Retry-After": str((retry_after_ms + 999) // 1000)},
    )
