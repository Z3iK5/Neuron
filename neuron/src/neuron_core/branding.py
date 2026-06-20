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
    <a href="/console">Admin console</a>
    <a href="/_matrix/client/versions">Client API</a>
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
  <div class="actions">
    <a class="btn-full primary" href="/console/settings">Set up your server</a>
    <a class="btn-full secondary" href="/get-started">Create another account</a>
  </div>
  {_connect_html(server_name)}"""
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


# Runs in <head> before paint: applies the saved theme (or OS preference on first
# visit) and the saved side-nav collapse state to <html>, so the admin console
# renders light/dark and expanded/collapsed with no flash. The toggle handlers
# (theme, nav collapse / mobile drawer) are defined here too. Harmless on the
# landing/auth pages, which keep their fixed brand colours (they read no tokens).
_HEAD_SCRIPT = (
    "<script>(function(){try{var h=document.documentElement,"
    "t=localStorage.getItem('neuron-theme');"
    "if(t!=='light'&&t!=='dark')t=window.matchMedia&&"
    "window.matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light';"
    "h.setAttribute('data-theme',t);"
    "if(localStorage.getItem('neuron-nav')==='collapsed')"
    "h.setAttribute('data-nav','collapsed');}catch(e){}})();"
    "function neuronToggleTheme(){var e=document.documentElement,"
    "n=e.getAttribute('data-theme')==='dark'?'light':'dark';"
    "e.setAttribute('data-theme',n);"
    "try{localStorage.setItem('neuron-theme',n);}catch(_){}}"
    "function neuronToggleNav(){var e=document.documentElement;"
    "if(window.innerWidth<=960){e.setAttribute('data-drawer',"
    "e.getAttribute('data-drawer')==='open'?'closed':'open');}"
    "else{var c=e.getAttribute('data-nav')==='collapsed'?'expanded':'collapsed';"
    "e.setAttribute('data-nav',c);"
    "try{localStorage.setItem('neuron-nav',c);}catch(_){}}}"
    "function neuronCloseDrawer(){"
    "document.documentElement.setAttribute('data-drawer','closed');}</script>"
)


def _shell(title: str, inner: str, *, body_class: str = "") -> str:
    cls = f' class="{body_class}"' if body_class else ""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<meta name="description" content="{DESCRIPTION}">'
        f'<link rel="icon" href="{favicon_data_uri()}">{FONTS_LINK}'
        f"<style>{_PAGE_CSS}</style>{_HEAD_SCRIPT}</head>"
        f"<body{cls}>{inner}</body></html>"
    )


# ---------------------------------------------------------------------------
# Admin console chrome (the merged neuron_server admin UI). Rendered as branded
# pure-Python HTML — same single source of truth as the landing/get-started pages,
# so no Jinja templates or static files are needed (which also keeps the frozen
# desktop bundle simple).
# ---------------------------------------------------------------------------

# Theme-toggle icons (generic line icons; the active one is chosen by CSS from
# the current [data-theme]). Moon shows in light mode, sun in dark mode.
_MOON_SVG = (
    '<svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
)
_SUN_SVG = (
    '<svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
    'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
)
_THEME_TOGGLE = (
    '<button type="button" class="theme-toggle" onclick="neuronToggleTheme()"'
    ' aria-label="Toggle light or dark theme" title="Toggle theme">'
    f"{_MOON_SVG}{_SUN_SVG}</button>"
)


def _icon(paths: str) -> str:
    """Wrap inner SVG ``paths`` in a 24x24 line-icon (generic geometry)."""
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        f' stroke-linecap="round" stroke-linejoin="round">{paths}</svg>'
    )


_HAMBURGER = _icon(
    '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/>'
    '<line x1="3" y1="18" x2="21" y2="18"/>'
)

# Side-nav: (href, label, icon). ``active`` matches the href.
_CONSOLE_NAV: tuple[tuple[str, str, str], ...] = (
    ("/console", "Overview", _icon(
        '<rect x="3" y="3" width="7" height="7" rx="1"/>'
        '<rect x="14" y="3" width="7" height="7" rx="1"/>'
        '<rect x="14" y="14" width="7" height="7" rx="1"/>'
        '<rect x="3" y="14" width="7" height="7" rx="1"/>')),
    ("/console/users", "Users", _icon(
        '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>'
        '<circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/>'
        '<path d="M16 3.13a4 4 0 0 1 0 7.75"/>')),
    ("/console/rooms", "Rooms", _icon(
        '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>')),
    ("/console/invites", "Invites", _icon(
        '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>')),
    ("/console/reports", "Reports", _icon(
        '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/>'
        '<line x1="4" y1="22" x2="4" y2="15"/>')),
    ("/console/passkeys", "Passkeys", _icon(
        '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6"/>'
        '<path d="m15.5 7.5 3 3L22 7l-3-3"/>')),
)


def login_card_html(
    server_name: str,
    *,
    csrf_token: str,
    error: str | None = None,
    username: str = "",
    next_url: str = "/console",
    passkey_button: bool = False,
    script: str = "",
) -> str:
    """The branded 'Sign in to your homeserver' card (the merged console login).

    Authenticates the operator's own **admin account** (Matrix username + password),
    matching the brand's login lockup. When ``passkey_button`` is set, a "Sign in
    with a passkey" button is shown and ``script`` (the WebAuthn JS) is injected.
    """
    err = f'<div class="error">{html.escape(error)}</div>' if error else ""
    safe_name = html.escape(server_name)
    passkey_html = (
        '<div class="or">or</div>'
        '<button type="button" id="pk-login" class="btn-full secondary">'
        "Sign in with a passkey</button>"
        '<div class="error" id="pk-login-err" hidden></div>'
        if passkey_button
        else ""
    )
    body = f"""<h2>Sign in to your homeserver</h2>{err}
  <form method="post" action="/console/login">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
    <input type="hidden" name="next" value="{html.escape(next_url)}">
    <label for="u">Username</label>
    <input id="u" name="username" value="{html.escape(username)}" placeholder="you"
      autocapitalize="none" autocorrect="off" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" placeholder="your password" required>
    <button type="submit">Sign in</button>
  </form>{passkey_html}
  <p class="note" style="margin-top:.85rem">Use the admin account for
    <code>{safe_name}</code> (the first account you created).</p>"""
    inner = f'<div class="card">{_card_head()}<div class="card-body">{body}</div></div>{script}'
    return _shell(f"Sign in · {NAME}", inner)


def admin_shell(
    title: str,
    body: str,
    *,
    active: str,
    server_name: str,
    flash: str | None = None,
) -> str:
    """Wrap console page ``body`` in the branded app-bar + side-nav + content layout."""
    def _nav_link(href: str, label: str, icon: str) -> str:
        cls = "navitem active" if href == active else "navitem"
        return (
            f'<a class="{cls}" href="{href}" title="{html.escape(label)}">'
            f'<span class="ico">{icon}</span><span class="lbl">{label}</span></a>'
        )

    nav = "".join(_nav_link(href, label, icon) for href, label, icon in _CONSOLE_NAV)
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    inner = (
        '<header class="appbar">'
        '<button type="button" class="hamburger" onclick="neuronToggleNav()"'
        f' aria-label="Toggle navigation">{_HAMBURGER}</button>'
        f'<a class="brand" href="/console"><span class="mark">{mark_svg(WHITE)}</span>'
        f'<span class="name">{NAME}</span></a>'
        '<span class="appbar-spacer"></span>'
        f"{_THEME_TOGGLE}"
        '<a class="host-tag" href="/console/settings" title="Server settings">'
        f"{html.escape(server_name)}</a>"
        '<a class="out" href="/console/logout">Sign out</a></header>'
        f'<div class="layout"><nav class="sidenav">{nav}</nav>'
        '<div class="scrim" onclick="neuronCloseDrawer()"></div>'
        f'<main class="content"><div class="wrap">{flash_html}{body}</div></main></div>'
    )
    return _shell(f"{title} · {NAME}", inner, body_class="admin")


_PAGE_CSS = """
*{box-sizing:border-box}
/* === design tokens (admin console) — light is default, dark swaps under
   [data-theme="dark"]. Landing/auth pages keep their fixed brand colours. === */
:root{
  --font-serif:'Cinzel',Georgia,'Times New Roman',serif;
  --font-sans:'Jost',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --font-mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  --radius-card:14px;--radius-control:9px;
  --appbar-h:54px;--nav-w:230px;--nav-w-collapsed:62px;--dur:.18s;
  --bg:#E4E2DC;--surface:#fff;--surface-sunken:#ECEAE4;
  --appbar-bg:#0E2740;--appbar-fg:#fff;
  --nav-fg:#B7C6D6;--nav-fg-hover:#fff;--nav-active-bg:#1C3D5F;--nav-active-fg:#fff;
  --primary:#1C3D5F;--primary-hover:#0E2740;--primary-contrast:#fff;--accent:#7FA8CC;
  --text:#16324F;--text-muted:#5A6B7C;--text-soft:#7C8896;--host-fg:#8FA6BC;
  --divider:#EDEBE5;--border:#E2E0D9;--row-divider:#F0EEE8;--hover:#FAF9F6;
  --success:#1a6b3a;--success-bg:#e8f3ec;--success-bd:#b9dcc6;
  --warning:#946200;--warning-bg:#fdf3e2;--error:#b00020;--error-bg:#fdecef;
  --danger:#b00020;  /* danger button bg — readable with white text in both themes */
  --shadow-1:0 1px 3px rgba(20,48,77,.06);--shadow-8:0 6px 24px rgba(14,39,64,.18);
}
[data-theme="dark"]{
  --bg:#0E1A26;--surface:#15273A;--surface-sunken:#0B1620;
  --appbar-bg:#0B1622;--appbar-fg:#EAF1F8;
  --nav-fg:#8FA6BC;--nav-fg-hover:#fff;--nav-active-bg:#1C3D5F;--nav-active-fg:#fff;
  --primary:#7FA8CC;--primary-hover:#9FBDD9;--primary-contrast:#0B1622;--accent:#9FBDD9;
  --text:rgba(234,241,248,.92);--text-muted:rgba(234,241,248,.66);
  --text-soft:rgba(234,241,248,.5);--host-fg:#8FA6BC;
  --divider:rgba(143,166,188,.16);--border:rgba(143,166,188,.22);
  --row-divider:rgba(143,166,188,.12);--hover:rgba(143,166,188,.08);
  --success:#6FD79A;--success-bg:rgba(26,107,58,.22);--success-bd:rgba(111,215,154,.3);
  --warning:#E6B96B;--warning-bg:rgba(148,98,0,.22);
  --error:#F38A98;--error-bg:rgba(176,0,32,.26);--danger:#B23A48;
  --shadow-1:0 1px 2px rgba(0,0,0,.5);--shadow-8:0 8px 28px rgba(0,0,0,.6);
}
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
.actions{display:flex;flex-direction:column;gap:.6rem;margin-top:1.3rem}
.btn-full{display:block;width:100%;text-align:center;padding:.72rem;border-radius:10px;
  font-size:1rem;font-weight:500;letter-spacing:.03em;text-decoration:none}
.btn-full.primary{background:#1C3D5F;color:#fff}
.btn-full.primary:hover{background:#0E2740}
.btn-full.secondary{background:#ECEAE4;color:#1C3D5F}
.btn-full.secondary:hover{background:#DEDBD2}
.or{text-align:center;color:#9aa0a8;font-size:.82rem;margin:.8rem 0 .6rem}

/* --- admin console (body.admin) --- */
body.admin{display:block;align-items:stretch;justify-content:flex-start;padding:0;
  background:var(--bg);color:var(--text);font-weight:400;min-height:100vh}
.appbar{position:sticky;top:0;z-index:30;height:var(--appbar-h);background:var(--appbar-bg);
  color:var(--appbar-fg);display:flex;align-items:center;gap:12px;padding:0 16px;
  box-shadow:var(--shadow-1)}
.appbar .brand{display:flex;align-items:center;gap:10px;text-decoration:none;
  color:var(--appbar-fg)}
.appbar .brand .mark{width:26px;height:26px;color:var(--appbar-fg);display:block}
.appbar .brand .name{font-family:var(--font-serif);font-weight:600;letter-spacing:.12em;
  font-size:18px}
.appbar-spacer{flex:1}
.appbar a.host-tag{color:var(--host-fg);font-size:12.5px;text-decoration:none;
  font-family:var(--font-mono)}
.appbar a.host-tag:hover{color:var(--appbar-fg);text-decoration:underline}
.appbar .out{color:var(--host-fg);text-decoration:none;font-size:13px}
.appbar .out:hover{color:var(--appbar-fg)}
.hamburger,.theme-toggle{background:transparent;border:none;color:var(--nav-fg);
  cursor:pointer;width:auto;padding:6px;border-radius:8px;display:inline-flex;
  align-items:center;line-height:0}
.hamburger:hover,.theme-toggle:hover{background:rgba(143,166,188,.16);
  color:var(--nav-fg-hover)}
.hamburger svg{width:20px;height:20px;display:block}
.theme-toggle svg{width:18px;height:18px;display:block}
.theme-toggle .icon-sun{display:none}
[data-theme="dark"] .theme-toggle .icon-sun{display:block}
[data-theme="dark"] .theme-toggle .icon-moon{display:none}
.layout{display:flex;align-items:flex-start}
.sidenav{flex:none;width:var(--nav-w);background:var(--appbar-bg);position:sticky;
  top:var(--appbar-h);height:calc(100vh - var(--appbar-h));overflow-y:auto;
  padding:14px 10px;display:flex;flex-direction:column;gap:2px;
  transition:width var(--dur) ease}
.navitem{display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:9px;
  color:var(--nav-fg);text-decoration:none;font-size:14px;position:relative;
  white-space:nowrap}
.navitem .ico{width:20px;height:20px;flex:none;display:inline-flex;align-items:center}
.navitem .ico svg{width:20px;height:20px;display:block}
.navitem:hover{background:rgba(143,166,188,.14);color:var(--nav-fg-hover)}
.navitem.active{background:var(--nav-active-bg);color:var(--nav-active-fg)}
.navitem.active::before{content:"";position:absolute;left:2px;top:8px;bottom:8px;width:3px;
  border-radius:3px;background:var(--accent)}
.content{flex:1;min-width:0}
.scrim{display:none}
[data-nav="collapsed"] .sidenav{width:var(--nav-w-collapsed)}
[data-nav="collapsed"] .navitem{justify-content:center;padding:9px}
[data-nav="collapsed"] .navitem .lbl{display:none}
.wrap{max-width:1040px;margin:0 auto;padding:30px 28px 64px;width:100%}
@media (max-width:960px){
  .sidenav{position:fixed;top:var(--appbar-h);left:0;z-index:40;width:var(--nav-w);
    transform:translateX(-100%)}
  [data-drawer="open"] .sidenav{transform:none;box-shadow:var(--shadow-8)}
  [data-nav="collapsed"] .navitem{justify-content:flex-start;padding:9px 12px}
  [data-nav="collapsed"] .navitem .lbl{display:inline}
  [data-drawer="open"] .scrim{display:block;position:fixed;inset:var(--appbar-h) 0 0 0;
    z-index:35;background:rgba(0,0,0,.45)}
  .wrap{padding:22px 16px 56px}
  .panel{overflow-x:auto}
}
@media (max-width:560px){.appbar a.host-tag{display:none}}
.flash{background:var(--success-bg);border:1px solid var(--success-bd);color:var(--success);
  padding:.7rem .9rem;border-radius:10px;margin-bottom:1.2rem;font-size:.93rem}
h1.page{font-family:var(--font-serif);font-weight:600;color:var(--primary);font-size:1.5rem;
  margin:0 0 1.1rem}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-card);
  padding:20px 22px;margin-bottom:20px;box-shadow:var(--shadow-1)}
.panel h2{font-family:var(--font-serif);font-weight:600;color:var(--primary);font-size:1.05rem;
  margin:0 0 .9rem}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;
  margin-bottom:22px}
.stat{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-card);padding:18px 20px}
.stat .num{font-size:2rem;font-weight:600;color:var(--primary);font-family:var(--font-serif);
  line-height:1}
.stat .lbl{color:var(--text-muted);font-size:.78rem;letter-spacing:.05em;
  text-transform:uppercase;margin-bottom:8px}
.tbl{width:100%;border-collapse:collapse;font-size:.93rem}
.tbl th{text-align:left;color:var(--text-soft);font-weight:500;font-size:.76rem;
  letter-spacing:.05em;text-transform:uppercase;padding:0 12px 9px;
  border-bottom:1px solid var(--divider)}
.tbl td{padding:10px 12px;border-bottom:1px solid var(--row-divider);color:var(--text)}
.tbl tr:hover td{background:var(--hover)}
.tbl a{color:var(--primary);text-decoration:none;font-weight:500}
.tbl a:hover{text-decoration:underline}
.btn{display:inline-block;width:auto;background:var(--primary);color:var(--primary-contrast);
  border:none;border-radius:var(--radius-control);padding:.5rem 1rem;font-size:.9rem;
  font-weight:500;cursor:pointer;text-decoration:none;font-family:inherit}
.btn:hover{background:var(--primary-hover)}
.btn.sm{padding:.34rem .68rem;font-size:.82rem}
.btn.ghost{background:var(--surface-sunken);color:var(--primary)}
.btn.ghost:hover{background:var(--border)}
.btn.danger{background:var(--danger);color:#fff}
.btn.danger:hover{filter:brightness(.93)}
.btn[disabled],.btn.disabled{opacity:.4;cursor:not-allowed;pointer-events:none}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.spread{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.muted{color:var(--text-soft);font-size:.88rem}
.pill{display:inline-block;padding:.1em .6em;border-radius:999px;font-size:.76rem;font-weight:500}
.pill.on{background:var(--success-bg);color:var(--success)}
.pill.off{background:var(--surface-sunken);color:var(--text-soft)}
.pill.warn{background:var(--error-bg);color:var(--error)}
.pill.amber{background:var(--warning-bg);color:var(--warning)}
form.inline{display:inline;margin:0}
.admin input.q{width:auto;display:inline-block;margin:0;max-width:260px}
.admin input,.admin textarea{background:var(--surface);color:var(--text);
  border:1px solid var(--border);border-radius:var(--radius-control);font-family:inherit}
.admin select{width:auto;display:inline-block;margin:0;padding:.5rem .6rem;
  border:1px solid var(--border);border-radius:9px;font-family:inherit;color:var(--text);
  background:var(--surface)}
.admin code{background:var(--surface-sunken);color:var(--primary);padding:.12em .45em;
  border-radius:5px;font-family:var(--font-mono);font-size:.9em}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:7px 18px;font-size:.93rem;margin:0}
.kv dt{color:var(--text-soft)}
.kv dd{margin:0;color:var(--text);font-family:var(--font-mono)}
.soon{font-size:.78rem;color:var(--text-soft);font-style:italic;margin-left:6px}
/* --- list primitives (toolbar / sortable / bulk / pager / empty) --- */
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.toolbar .grow{flex:1}
.tbl th.sortable a{color:inherit;text-decoration:none;display:inline-flex;
  align-items:center;gap:5px}
.tbl th.sortable a:hover{color:var(--text)}
.tbl th .arrow{font-size:.7em;opacity:.75}
.bulkbar{display:flex;align-items:center;gap:12px;background:var(--surface-sunken);
  border:1px solid var(--border);border-radius:var(--radius-control);padding:8px 14px;
  margin-bottom:14px;font-size:.9rem;color:var(--text)}
.pager{display:flex;align-items:center;gap:14px;margin-top:16px;color:var(--text-muted);
  font-size:.88rem}
.pager a{color:var(--primary);text-decoration:none}
.pager a:hover{text-decoration:underline}
.empty{text-align:center;color:var(--text-muted);padding:40px 20px}
.empty .big{font-size:1.05rem;color:var(--text);margin-bottom:6px}
"""
