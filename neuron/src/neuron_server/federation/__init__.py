# SPDX-License-Identifier: Apache-2.0
"""Server-to-server (federation) support for neuron_server (HS-7)."""

from neuron_server.federation.auth import (
    XMatrixCredentials,
    parse_authorization_header,
    sign_request,
    verify_request,
)

__all__ = [
    "XMatrixCredentials",
    "parse_authorization_header",
    "sign_request",
    "verify_request",
]
