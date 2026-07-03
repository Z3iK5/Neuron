# SPDX-License-Identifier: Apache-2.0
"""Render the NEURON app icon (the Neural Shield mark on a navy squircle).

Uses the exact mark geometry from :mod:`neuron_core.branding` so the raster icon
matches the SVG used on the web. Shared by the tray app and the installer-icon
generator.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from neuron_core import branding

# Hex colours → RGB.
_NAVY = (0x1C, 0x3D, 0x5F)
_WHITE = (0xFF, 0xFF, 0xFF)
_SUPERSAMPLE = 4  # render large, then downscale for clean anti-aliasing


def render_icon(
    size: int = 512,
    *,
    background: tuple[int, int, int] | None = _NAVY,
    foreground: tuple[int, int, int] = _WHITE,
    padding_ratio: float = 0.2,
) -> Image.Image:
    """Return the app icon as an RGBA :class:`PIL.Image.Image` of ``size`` px.

    ``background=None`` yields a transparent icon (just the mark in ``foreground``).
    """
    viewbox = branding.MARK_VIEWBOX
    outer_nodes = branding.OUTER_NODES
    center = branding.CENTER
    outer_r = branding.OUTER_RADIUS
    center_r = branding.CENTER_RADIUS
    stroke = branding.STROKE

    big = size * _SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if background is not None:
        radius = int(big * 0.24)
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=(*background, 255))

    pad = big * padding_ratio
    scale = (big - 2 * pad) / viewbox

    def place(point: tuple[float, float]) -> tuple[float, float]:
        return (pad + point[0] * scale, pad + point[1] * scale)

    nodes = [place(p) for p in outer_nodes]
    cx, cy = place(center)
    line_w = max(1, int(round(stroke * scale)))
    fg = (*foreground, 255)

    # Hexagon edges + spokes from the centre (square caps are hidden by the nodes).
    for i, node in enumerate(nodes):
        draw.line([node, nodes[(i + 1) % len(nodes)]], fill=fg, width=line_w)
        draw.line([(cx, cy), node], fill=fg, width=line_w)

    def disc(point: tuple[float, float], r: float) -> None:
        x, y = point
        rr = r * scale
        draw.ellipse((x - rr, y - rr, x + rr, y + rr), fill=fg)

    for node in nodes:
        disc(node, outer_r)
    disc((cx, cy), center_r)

    return image.resize((size, size), Image.Resampling.LANCZOS)
