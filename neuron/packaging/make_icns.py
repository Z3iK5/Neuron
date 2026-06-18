# SPDX-License-Identifier: Apache-2.0
"""Generate ``packaging/icons/neuron.icns`` for the macOS ``.app`` bundle.

macOS app icons are ``.icns`` files built from an *iconset* — a folder of PNGs at
fixed sizes. We render the NEURON mark at those sizes (from the single source of
truth in :mod:`neuron_core.branding`, via :func:`neuron_desktop.icon.render_icon`)
and let the system ``iconutil`` assemble the ``.icns``.

Run on macOS, from the ``neuron/`` directory::

    python packaging/make_icns.py

On any other platform this is a no-op: the ``.icns`` is only needed when building
the macOS bundle, and the PyInstaller spec falls back gracefully without it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from neuron_desktop.icon import render_icon

_HERE = Path(__file__).resolve().parent
_OUT = _HERE / "icons" / "neuron.icns"

# (pixel size, Apple iconset filename) — the standard set Retina macOS expects.
_ICONSET: tuple[tuple[int, str], ...] = (
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
)


def main() -> int:
    if sys.platform != "darwin":
        print("make_icns: not macOS — skipping (.icns is only needed there)")
        return 0
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "neuron.iconset"
        iconset.mkdir()
        for size, name in _ICONSET:
            render_icon(size).save(iconset / name)
        subprocess.run(
            ["iconutil", "--convert", "icns", "--output", str(_OUT), str(iconset)],
            check=True,
        )
    print(f"make_icns: wrote {_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
