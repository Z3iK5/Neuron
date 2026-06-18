# SPDX-License-Identifier: Apache-2.0
"""Generate the MSIX logo assets from the NEURON brand mark.

MSIX packages reference a few fixed-size PNG logos. We render them from the single
source of truth (:func:`neuron_desktop.icon.render_icon`) so they match the rest of
the brand. Run from the ``neuron/`` directory:

    python packaging/make_msix_assets.py <output-dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

from neuron_desktop.icon import render_icon

# (filename, pixel size) — the logos referenced by AppxManifest.template.xml.
_ASSETS: tuple[tuple[str, int], ...] = (
    ("StoreLogo.png", 50),
    ("Square44x44Logo.png", 44),
    ("Square150x150Logo.png", 150),
)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: make_msix_assets.py <output-dir>", file=sys.stderr)
        return 2
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, size in _ASSETS:
        render_icon(size).save(out / name)
    print(f"make_msix_assets: wrote {len(_ASSETS)} logos to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
