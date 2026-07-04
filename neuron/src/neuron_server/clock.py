# SPDX-License-Identifier: Apache-2.0
"""Wall-clock helpers shared across ``neuron_server``."""

from __future__ import annotations

import time


def now_ms() -> int:
    """The current wall-clock time in milliseconds since the epoch."""
    return int(time.time() * 1000)
