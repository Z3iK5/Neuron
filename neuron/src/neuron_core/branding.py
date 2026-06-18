# SPDX-License-Identifier: Apache-2.0
"""The NEURON brand — single source of truth for the mark, palette and type.

Concept 1, *Neural Shield*: a hexagon of six nodes wired to a central node. The
mark is defined once here (geometry + SVG) and reused everywhere — the homeserver
landing page, the admin console, the desktop app icon, and the repository assets —
so the brand stays consistent across surfaces.
"""

from __future__ import annotations

import html
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
    inner = f"""<div class="hero">
  <div class="mark">{mark_svg(WHITE)}</div>
  <div><h1 class="name">{NAME}</h1><div class="tag">{TAGLINE}</div></div>
  <p class="desc">{DESCRIPTION}</p>
  <a class="cta" href="/get-started">Get started</a>
  <div class="host">Matrix homeserver for <code>{html.escape(server_name)}</code></div>
  <div class="links">
    <a href="/_matrix/client/versions">Client API</a>
    <a href="/_matrix/key/v2/server">Server keys</a>
    <a href="https://github.com/Z3iK5/Neuron">Source</a>
  </div>
</div>"""
    return _shell(f"{NAME} · {server_name}", inner)


def get_started_html(
    server_name: str,
    *,
    can_register: bool,
    token: str | None = None,
    error: str | None = None,
    username: str = "",
) -> str:
    """The 'Get started' page: create an account (if allowed) + connect-a-client guide.

    ``can_register`` is the computed gate — true when open registration is on *or* a
    valid invite ``token`` was supplied. A supplied ``token`` is carried through the
    form (hidden field) so the submission stays authorised.
    """
    if can_register:
        err = f'<div class="error">{html.escape(error)}</div>' if error else ""
        hidden = (
            f'<input type="hidden" name="token" value="{html.escape(token)}">'
            if token
            else ""
        )
        invited = (
            '<p class="note" style="margin-bottom:1rem">You were invited to this '
            "server. Create your account below.</p>"
            if token
            else ""
        )
        body = f"""<h2>Create your account</h2>{err}{invited}
  <form method="post" action="/get-started">{hidden}
    <label for="u">Username</label>
    <input id="u" name="username" value="{html.escape(username)}" placeholder="alice"
      autocapitalize="none" autocorrect="off" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" placeholder="choose a password" required>
    <button type="submit">Create account</button>
  </form>
  <p class="note" style="margin-top:.85rem">Your Matrix ID will be
    <code>@username:{html.escape(server_name)}</code>.</p>"""
    else:
        body = (
            '<h2>Accounts</h2><p class="note">Open registration is disabled on this '
            "server. Ask the administrator for an invite link, or to create an account "
            "for you, then connect a chat app below.</p>"
        )
    inner = (
        f'<div class="card">{_card_head()}<div class="card-body">'
        f"{body}{_connect_html(server_name)}</div></div>"
    )
    return _shell(f"Get started · {NAME}", inner)


def welcome_html(server_name: str, user_id: str) -> str:
    """The success page after an account is created in the browser."""
    body = f"""<div class="success">&#10003; Account created</div>
  <p class="note">This is your Matrix ID — sign in with it and your password:</p>
  <div class="idbox">{html.escape(user_id)}</div>
  {_connect_html(server_name)}
  <div class="foot"><a href="/get-started">Create another account</a></div>"""
    inner = f'<div class="card">{_card_head()}<div class="card-body">{body}</div></div>'
    return _shell(f"Welcome · {NAME}", inner)


def _card_head() -> str:
    return (
        f'<div class="card-head"><div class="mark">{mark_svg(WHITE)}</div>'
        f'<div><div class="name">{NAME}</div><div class="tag">{TAGLINE}</div></div></div>'
    )


def _connect_html(server_name: str) -> str:
    safe = html.escape(server_name)
    return f"""<div class="connect">
  <h2>Connect a chat app</h2>
  <p class="note">Your account works in any Matrix client. When it asks for a homeserver, enter:</p>
  <div class="idbox">{safe}</div>
  <ol>
    <li>Open a Matrix app — <a href="https://element.io/download">Element</a> or FluffyChat.</li>
    <li>Choose <strong>Sign in</strong> &rarr; <strong>Edit / Other homeserver</strong>
      and enter <code>{safe}</code>.</li>
    <li>Sign in with your Matrix ID and password.</li>
  </ol>
</div>"""


def _shell(title: str, inner: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<meta name="description" content="{DESCRIPTION}">'
        f'<link rel="icon" href="{favicon_data_uri()}">{FONTS_LINK}'
        f"<style>{_PAGE_CSS}</style></head><body>{inner}</body></html>"
    )


_PAGE_CSS = """
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{background:#0E2740;color:#fff;font-family:'Jost',system-ui,-apple-system,sans-serif;
  font-weight:300;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:40px}
a{color:#7FA8CC}
@keyframes neuronPulse{0%,100%{transform:scale(1);opacity:.94}50%{transform:scale(1.05);opacity:1}}
.hero{max-width:560px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:24px}
.hero .mark{width:104px;height:104px;color:#fff;animation:neuronPulse 2.6s ease-in-out infinite}
.name{font-family:'Cinzel',Georgia,serif;font-weight:600;letter-spacing:.1em;font-size:46px;
  line-height:1;margin:0}
.tag{letter-spacing:.34em;text-transform:lowercase;color:#8FA6BC;font-size:14px;margin-top:14px}
.desc{color:#B7C6D6;font-size:17px;line-height:1.6;max-width:30em;margin:0}
.host{font-size:14px;color:#8FA6BC}
.host code{color:#fff;background:rgba(143,166,188,.16);padding:.15em .5em;border-radius:6px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}
.links{display:flex;gap:22px;font-size:13px;letter-spacing:.04em}
.links a{text-decoration:none}
.cta{display:inline-block;background:#7FA8CC;color:#0E2740;font-weight:500;text-decoration:none;
  padding:.7rem 1.6rem;border-radius:10px;letter-spacing:.04em}
.cta:hover{background:#9FB9D6}
.card{background:#fff;color:#16324F;width:100%;max-width:460px;border-radius:18px;overflow:hidden;
  box-shadow:0 18px 50px rgba(0,0,0,.32)}
.card-head{background:#0E2740;color:#fff;padding:2rem 2rem 1.6rem;text-align:center;
  display:flex;flex-direction:column;align-items:center;gap:.75rem}
.card-head .mark{width:52px;height:52px;color:#fff}
.card-head .name{font-family:'Cinzel',Georgia,serif;font-weight:600;letter-spacing:.12em;
  font-size:1.7rem}
.card-head .tag{margin-top:0}
.card-body{padding:1.6rem 1.9rem 2rem}
.card-body h2{font-family:'Cinzel',Georgia,serif;font-weight:600;color:#1C3D5F;font-size:1.15rem;
  margin:0 0 1rem}
label{display:block;font-weight:500;margin:0 0 .3rem;font-size:.92rem;color:#16324F}
input{width:100%;padding:.6rem .75rem;border:1px solid #DEDCD6;border-radius:10px;font-size:1rem;
  margin-bottom:1rem;font-family:inherit;color:#16324F}
button{width:100%;background:#1C3D5F;color:#fff;border:none;border-radius:10px;padding:.72rem;
  font-size:1rem;font-weight:500;letter-spacing:.03em;cursor:pointer}
button:hover{background:#0E2740}
.error{background:#fdecef;border:1px solid #f5c2cb;color:#b00020;padding:.6rem .8rem;
  border-radius:10px;margin-bottom:1rem;font-size:.92rem}
.note{color:#5A6B7C;font-size:.92rem;line-height:1.55;margin:0}
.card-body code{color:#1C3D5F;background:#ECEAE4;padding:.12em .45em;border-radius:5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}
.connect{border-top:1px solid #EDEBE5;margin-top:1.4rem;padding-top:1.3rem}
.connect ol{margin:.5rem 0 0;padding-left:1.2rem;color:#16324F;line-height:1.7;font-size:.94rem}
.connect a{color:#1C3D5F}
.success{display:flex;align-items:center;gap:.5rem;color:#1a6b3a;font-weight:600;
  font-size:1.05rem;margin-bottom:.8rem}
.idbox{background:#ECEAE4;border:1px solid #DEDCD6;border-radius:10px;padding:.7rem .8rem;
  margin:.4rem 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#1C3D5F;
  word-break:break-all}
.foot{margin-top:1.4rem;font-size:.85rem;color:#7C8896;text-align:center}
.foot a{color:#5A6B7C}
"""
