# SPDX-License-Identifier: Apache-2.0
"""QR codes for the consoles — used to make invite links scannable from a phone.

Wraps :mod:`segno` (pure-Python, no native deps) and renders an inline SVG in the
NEURON palette so it drops straight into a server-rendered page or a standalone
``image/svg+xml`` response. ``segno`` ships with the ``console`` and ``server``
extras, so it is imported lazily to keep base ``neuron_core`` installs working.
"""

from __future__ import annotations

import io


def qr_svg(data: str, *, scale: int = 4, border: int = 2) -> str:
    """Render ``data`` as an inline SVG QR code (NEURON navy on white)."""
    import segno

    from neuron_core import branding

    qr = segno.make(data, error="m")
    buf = io.BytesIO()
    qr.save(
        buf,
        kind="svg",
        scale=scale,
        border=border,
        dark=branding.NAVY,
        light=branding.WHITE,
        xmldecl=False,
        svgns=True,
    )
    return buf.getvalue().decode("utf-8")
