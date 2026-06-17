# SPDX-License-Identifier: Apache-2.0
"""Canonical JSON for Matrix signatures.

Matrix signs the *canonical* JSON form of an object (keys sorted, no insignificant
whitespace), excluding the ``signatures`` and ``unsigned`` fields. Both the Olm
device and cross-signing keys sign objects this way.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: dict[str, Any]) -> str:
    """Return the Matrix canonical-JSON string used for signing ``obj``."""
    to_sign = {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    return json.dumps(to_sign, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
