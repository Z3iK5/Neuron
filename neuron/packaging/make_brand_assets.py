# SPDX-License-Identifier: Apache-2.0
"""Generate the committed NEURON brand asset files from the single source of truth.

Run from the ``neuron/`` directory:  python packaging/make_brand_assets.py

Emits, under ``assets/brand/`` (and the installer icon under ``packaging/icons/``):
  * neuron-mark.svg / neuron-mark-white.svg  — the Neural Shield mark
  * neuron-icon.png (1024) / neuron-icon-512.png — the navy-squircle app icon
  * neuron.ico — multi-size Windows/installer icon
  * neuron-social.svg / neuron-social.png (1280x640) — social/repo banner

SVGs use the brand web fonts (Cinzel/Jost) and are the true-brand source. The PNG
banner is a raster export for GitHub's social preview (which must be raster) and
uses a bundled serif as a Cinzel stand-in.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from neuron_core import branding
from neuron_desktop.icon import render_icon

_ROOT = Path(__file__).resolve().parent.parent
_ASSETS = _ROOT / "assets" / "brand"
_ICONS = _ROOT / "packaging" / "icons"

# Optional fonts available in this environment, used only for the raster banner.
_SERIF_CANDIDATES = [
    "/mnt/skills/examples/canvas-design/canvas-fonts/YoungSerif-Regular.ttf",
    "/mnt/skills/examples/canvas-design/canvas-fonts/IBMPlexSerif-Bold.ttf",
]
_SANS_CANDIDATES = [
    "/usr/share/fonts/truetype/katex/KaTeX_SansSerif-Regular.ttf",
    "/mnt/skills/examples/canvas-design/canvas-fonts/IBMPlexSerif-Regular.ttf",
]


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print("wrote", path.relative_to(_ROOT))


def write_svgs() -> None:
    _write(_ASSETS / "neuron-mark.svg", branding.mark_svg(branding.NAVY))
    _write(_ASSETS / "neuron-mark-white.svg", branding.mark_svg(branding.WHITE))
    _write(_ASSETS / "neuron-social.svg", _social_svg())


def write_icons() -> None:
    _ASSETS.mkdir(parents=True, exist_ok=True)
    _ICONS.mkdir(parents=True, exist_ok=True)
    icon_1024 = render_icon(1024)
    icon_1024.save(_ASSETS / "neuron-icon.png")
    render_icon(512).save(_ASSETS / "neuron-icon-512.png")
    # Multi-size .ico for Windows / installers.
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon_1024.save(_ICONS / "neuron.ico", sizes=ico_sizes)
    icon_1024.resize((256, 256), Image.Resampling.LANCZOS).save(_ICONS / "neuron.png")
    print("wrote", (_ASSETS / "neuron-icon.png").relative_to(_ROOT))
    print("wrote", (_ICONS / "neuron.ico").relative_to(_ROOT))


def _social_svg() -> str:
    mark = branding.mark_svg(branding.WHITE)
    faint = branding.mark_svg("rgba(143,166,188,0.10)")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640"\
 viewBox="0 0 1280 640">
<defs><style>
  .name{{font-family:'Cinzel',Georgia,serif;font-weight:600;letter-spacing:.08em;fill:#fff}}
  .tag{{font-family:'Jost',sans-serif;font-weight:300;letter-spacing:.34em;fill:#8FA6BC}}
  .desc{{font-family:'Jost',sans-serif;font-weight:300;fill:#B7C6D6}}
</style></defs>
<rect width="1280" height="640" fill="#0E2740"/>
<g transform="translate(900,320) scale(2.8)" opacity="1">
  <g transform="translate(-100,-100)">{faint}</g>
</g>
<g transform="translate(120,232)"><g transform="scale(0.44)">{mark}</g></g>
<text x="232" y="290" class="name" font-size="92">NEURON</text>
<text x="236" y="338" class="tag" font-size="22">matrix homeserver</text>
<text x="122" y="430" class="desc" font-size="30">Your private chat, on your own server.</text>
<text x="122" y="474" class="desc" font-size="30">Self-hosted Matrix, end-to-end encrypted.</text>
</svg>"""


def _spaced(
    draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font, fill, tracking: int
) -> None:
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += int(draw.textlength(ch, font=font)) + tracking


def write_social_png() -> None:
    deep = _rgb(branding.DEEP)
    img = Image.new("RGB", (1280, 640), deep)
    # Faint mark motif on the right.
    motif = render_icon(640, background=None, foreground=(143, 166, 188), padding_ratio=0.0)
    faint = Image.new("RGBA", motif.size, (0, 0, 0, 0))
    faint.paste(motif, (0, 0), motif)
    alpha = faint.split()[3].point(lambda a: int(a * 0.10))
    faint.putalpha(alpha)
    img.paste(faint, (820, 0), faint)
    # Foreground mark.
    mark = render_icon(150, background=None, foreground=(255, 255, 255), padding_ratio=0.0)
    img.paste(mark, (120, 150), mark)
    draw = ImageDraw.Draw(img)
    _spaced(draw, (300, 168), "NEURON", _font(_SERIF_CANDIDATES, 96), (255, 255, 255), 8)
    _spaced(draw, (304, 286), "matrix homeserver", _font(_SANS_CANDIDATES, 24), (143, 166, 188), 6)
    desc = _font(_SANS_CANDIDATES, 30)
    desc_fill = (183, 198, 214)
    draw.text((122, 420), "Your private chat, on your own server.", font=desc, fill=desc_fill)
    draw.text((122, 468), "Self-hosted Matrix, end-to-end encrypted.", font=desc, fill=desc_fill)
    _ASSETS.mkdir(parents=True, exist_ok=True)
    img.save(_ASSETS / "neuron-social.png")
    print("wrote", (_ASSETS / "neuron-social.png").relative_to(_ROOT))


def main() -> None:
    write_svgs()
    write_icons()
    write_social_png()


if __name__ == "__main__":
    main()
