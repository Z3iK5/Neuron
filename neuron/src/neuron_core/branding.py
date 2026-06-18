# SPDX-License-Identifier: Apache-2.0
"""The NEURON brand — single source of truth for the mark, palette and type.

Concept 1, *Neural Shield*: a hexagon of six nodes wired to a central node. The
mark is defined once here (geometry + SVG) and reused everywhere — the homeserver
landing page, the admin console, the desktop app icon, and the repository assets —
so the brand stays consistent across surfaces.
"""

from __future__ import annotations

import urllib.parse

# --- palette ---------------------------------------------------------------
NAVY = "#1C3D5F"  # primary
DEEP = "#0E2740"  # dark background
PAPER = "#ECEAE4"
CANVAS = "#E4E2DC"
WHITE = "#FFFFFF"
TEXT = "#16324F"
MUTED = "#5A6B7C"
MUTED_SOFT = "#7C8896"
ON_DARK = "#8FA6BC"
ACCENT = "#7FA8CC"
BORDER = "#DEDCD6"

# --- identity --------------------------------------------------------------
NAME = "NEURON"
TAGLINE = "matrix homeserver"
DESCRIPTION = (
    "Your private chat, on your own server. Self-hosted Matrix, end-to-end encrypted."
)

# --- typography ------------------------------------------------------------
FONTS_HREF = (
    "https://fonts.googleapis.com/css2?"
    "family=Cinzel:wght@400;500;600;700&family=Jost:wght@300;400;500;600&display=swap"
)
FONTS_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    f'<link href="{FONTS_HREF}" rel="stylesheet">'
)
SERIF = "'Cinzel', Georgia, 'Times New Roman', serif"
SANS = "'Jost', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"

# --- mark geometry (on a 200x200 viewBox) ----------------------------------
MARK_VIEWBOX = 200.0
_OUTER_NODES: tuple[tuple[float, float], ...] = (
    (100.0, 30.0),
    (160.6, 65.0),
    (160.6, 135.0),
    (100.0, 170.0),
    (39.4, 135.0),
    (39.4, 65.0),
)
_CENTER = (100.0, 100.0)
_OUTER_R = 14.0
_CENTER_R = 13.0
_STROKE = 8.0

# Public, typed view of the geometry so raster renderers (the desktop icon) match
# the SVG exactly.
OUTER_NODES: tuple[tuple[float, float], ...] = _OUTER_NODES
CENTER: tuple[float, float] = _CENTER
OUTER_RADIUS: float = _OUTER_R
CENTER_RADIUS: float = _CENTER_R
STROKE: float = _STROKE


def _n(value: float) -> str:
    return f"{value:g}"


def mark_svg(color: str = "currentColor", *, size: str | None = None) -> str:
    """The Neural Shield mark as an SVG string, drawn in ``color``."""
    dims = f' width="{size}" height="{size}"' if size else ' width="100%" height="100%"'
    points = " ".join(f"{_n(x)},{_n(y)}" for x, y in _OUTER_NODES)
    spokes = "".join(
        f'<line x1="{_n(_CENTER[0])}" y1="{_n(_CENTER[1])}" x2="{_n(x)}" y2="{_n(y)}"/>'
        for x, y in _OUTER_NODES
    )
    outer = "".join(
        f'<circle cx="{_n(x)}" cy="{_n(y)}" r="{_n(_OUTER_R)}"/>' for x, y in _OUTER_NODES
    )
    return (
        f'<svg viewBox="0 0 {_n(MARK_VIEWBOX)} {_n(MARK_VIEWBOX)}"{dims} fill="none"'
        ' xmlns="http://www.w3.org/2000/svg" role="img" aria-label="NEURON">'
        f'<g stroke="{color}" stroke-width="{_n(_STROKE)}" stroke-linecap="round"'
        f' stroke-linejoin="round"><polygon points="{points}"/>{spokes}</g>'
        f'<g fill="{color}"><circle cx="{_n(_CENTER[0])}" cy="{_n(_CENTER[1])}"'
        f' r="{_n(_CENTER_R)}"/>{outer}</g></svg>'
    )


def favicon_data_uri(color: str = NAVY) -> str:
    """An ``<svg>`` favicon as a ``data:`` URI (for ``<link rel="icon">``)."""
    return "data:image/svg+xml," + urllib.parse.quote(mark_svg(color))


def landing_page_html(server_name: str) -> str:
    """A branded landing page for a homeserver, served at ``GET /``."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{NAME} · {server_name}</title>
<meta name="description" content="{DESCRIPTION}">
<link rel="icon" href="{favicon_data_uri()}">
{FONTS_LINK}
<style>
  *{{box-sizing:border-box}}
  html,body{{margin:0;height:100%}}
  body{{background:{DEEP};color:{WHITE};font-family:{SANS};font-weight:300;
    display:flex;align-items:center;justify-content:center;min-height:100vh;padding:40px}}
  @keyframes neuronPulse{{
    0%,100%{{transform:scale(1);opacity:.94}}50%{{transform:scale(1.05);opacity:1}}}}
  .wrap{{max-width:560px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:26px}}
  .mark{{width:104px;height:104px;color:{WHITE};animation:neuronPulse 2.6s ease-in-out infinite}}
  .name{{font-family:{SERIF};font-weight:600;letter-spacing:.1em;font-size:46px;line-height:1;margin:0}}
  .tag{{letter-spacing:.34em;text-transform:lowercase;color:{ON_DARK};font-size:14px;margin-top:14px}}
  .desc{{color:#B7C6D6;font-size:17px;line-height:1.6;max-width:30em;margin:0}}
  .host{{margin-top:6px;font-size:14px;color:{ON_DARK}}}
  .host code{{color:{WHITE};background:rgba(143,166,188,.14);padding:.15em .5em;border-radius:6px;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
  .links{{display:flex;gap:22px;margin-top:6px;font-size:13px;letter-spacing:.04em}}
  .links a{{color:{ACCENT};text-decoration:none}}
  .links a:hover{{text-decoration:underline}}
</style>
</head>
<body>
  <div class="wrap">
    <div class="mark">{mark_svg(WHITE)}</div>
    <div>
      <h1 class="name">{NAME}</h1>
      <div class="tag">{TAGLINE}</div>
    </div>
    <p class="desc">{DESCRIPTION}</p>
    <div class="host">This is the Matrix homeserver for <code>{server_name}</code>.</div>
    <div class="links">
      <a href="/_matrix/client/versions">Client API</a>
      <a href="/_matrix/key/v2/server">Server keys</a>
      <a href="https://github.com/Z3iK5/Neuron">Source</a>
    </div>
  </div>
</body>
</html>"""
