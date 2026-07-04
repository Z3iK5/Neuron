# SPDX-License-Identifier: Apache-2.0
"""QR codes for the console — used to make invite links scannable from a phone.

The implementation lives in :mod:`neuron_core.qr`; this module re-exports it so
console call sites keep their existing import path.
"""

from __future__ import annotations

from neuron_core.qr import qr_svg

__all__ = ["qr_svg"]
