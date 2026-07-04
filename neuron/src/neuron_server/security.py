# SPDX-License-Identifier: Apache-2.0
"""CSRF protection for the admin console's state-changing forms.

The implementation (the classic synchroniser-token pattern) lives in
:mod:`neuron_core.security` — the shared library both the merged
homeserver+console app and the standalone console depend on. This module
re-exports it so server call sites keep their existing import path.
"""

from __future__ import annotations

from neuron_core.security import get_csrf_token, verify_csrf

__all__ = ["get_csrf_token", "verify_csrf"]
