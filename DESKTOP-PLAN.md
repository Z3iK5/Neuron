# Neuron Desktop — Installable Server App Plan

> **Goal:** let anyone run their own Neuron homeserver as an installed
> **app/program** on **macOS, Windows, and Linux** — not only via Docker.
> Additive to the Docker path, which remains for headless/server deployments.

## Decisions recorded (from review)

| Decision | Choice |
|---|---|
| Product shape | **Menu-bar / system-tray control app** (Start/Stop, first-run setup, "Open Console") |
| Primary use | **Both — local-first**: dead-simple personal/LAN now; public-internet as a documented upgrade |
| Management UI | Reuse the existing **web admin console** (no second UI to build) |
| Server runtime | The existing `neuron_server` (FastAPI/uvicorn), default **SQLite**, per-user data dir |

## Why this is a good fit

- `neuron_server` is **pure-Python**, defaults to a **single SQLite file**, and is a
  self-contained ASGI app — ideal for bundling.
- The **server does not need `libolm`**: it only *relays* E2EE keys (HS-5), never
  decrypts. So the hardest native dependency is absent from the server binary.
- We already ship a **web admin console**, so the desktop app only needs to be a
  thin *supervisor* (start/stop/status + first-run wizard), not a new GUI.

## Architecture

```
   ┌──────────────── Neuron Desktop (installed app) ─────────────────┐
   │  Tray/menu-bar control  ── Start · Stop · Open Console · Quit    │
   │        │                     · Open data folder · Status         │
   │        ▼                                                         │
   │  Supervisor: spawns/monitors the server process, first-run setup │
   │        │                                                         │
   │        ▼                                                         │
   │  neuron_server (uvicorn, 127.0.0.1:8008, SQLite in app data dir) │
   │        ▲  browser → http://localhost:8008  (web admin console)   │
   └─────────┴───────────────────────────────────────────────────────┘
   State (DB, media, signing keys, config) lives in the per-user app data dir.
```

Per-user data dir via `platformdirs`:
- macOS `~/Library/Application Support/Neuron`
- Windows `%APPDATA%\Neuron`
- Linux `~/.local/share/neuron`

## Phased plan (each phase independently shippable)

- **D0 — Installable CLI + data dir.** `[project.scripts]` `neuron-server` entry
  point so `pipx install neuron` / `pip install` yields a real cross-platform
  command today. Default the DB/media/keys to the per-user app data dir (override
  with env vars unchanged). **Done when:** `pipx install` then `neuron-server`
  runs a server storing state in the app data dir. *(S — partly done this commit)*
- **D1 — First-run setup + supervisor. ✅ Done.** The `neuron_desktop` package:
  `paths` (per-user data dir via `platformdirs`, `NEURON_DATA_DIR` override),
  `config` (`config.json` ↔ a fully-derived `NeuronServerSettings` pointing the DB,
  media and signing key at the data dir), `setup` (first-run detection, interactive
  wizard with injectable I/O, idempotent admin-account creation), `supervisor`
  (`serve`, `open_console`), and a `neuron-desktop` CLI (`run`/`setup`/`where`/
  `console`). **Done:** a fresh data dir goes launch → admin account → the admin
  signs in and uses the admin API, all verified by unit tests (the GUI/installer
  parts remain D2/D3). Data-dir defaulting (the unfinished part of D0) is handled
  here at the desktop layer, keeping `neuron_server` itself env-configured and pure.
- **D2 — Tray / menu-bar app.** Cross-platform tray icon (`pystray`) with
  Start/Stop, Open Console, Open data folder, Status, Quit; server runs in a
  background process. **Done when:** the app sits in the tray and controls the
  server. *(M — GUI verified on real OSes, not in CI headless)*
- **D3 — Native installers via CI.** GitHub Actions **matrix** (macOS, Windows,
  Ubuntu) packaging with **PyInstaller** or **Briefcase** → `.dmg`/`.app`,
  `.msi`/`.exe`, `AppImage`/`.deb`; uploaded as release artifacts. **Done when:**
  a tagged release produces downloadable installers for all three OSes. *(L —
  must build on each OS; CI matrix handles it; no cross-compile)*
- **D4 — Trust & polish.** Code signing + notarization (Apple Developer ID;
  Windows Authenticode), optional auto-update, "start at login" toggle. *(M, has
  external costs — see risks)*
- **D5 — Public-server path (local-first upgrade).** Documented path: set the
  server name to a real domain, bundle/automate TLS (Caddy or an ACME client),
  serve `.well-known`, firewall/port guidance. Ties into federation (HS-7). *(L,
  optional)*

## Honest constraints & risks

- **Build-on-each-OS.** PyInstaller/Briefcase can't cross-compile; we build on
  macOS/Windows/Linux CI runners. (GitHub Actions provides all three.)
- **In-container limits.** GUI/tray behavior and the actual `.dmg`/`.msi`/AppImage
  installers can only be validated in CI and on real machines — not in the Linux
  dev container. Launcher/setup *logic* is unit-tested here.
- **Code signing has real cost.** Without it, macOS Gatekeeper and Windows
  SmartScreen warn users. Apple Developer ID ≈ $99/yr; Windows needs a signing
  cert. Treated as a deliberate D4 step, not assumed.
- **Public servers are inherently more setup.** Domain + TLS + an open port (and
  federation, HS-7) can't be fully one-click; DNS/port-forwarding stay manual. The
  local/LAN case (default) *is* one-click.
- **Bundle size** ≈ tens of MB (Python + FastAPI + uvicorn + Pillow). Fine.

## Sequencing vs. the homeserver roadmap

The polished desktop experience is best after **HS-6** (when the admin console
manages `neuron_server` natively). But D0 is independent and useful now; D1–D2 can
be built in parallel. Recommendation: land D0 now, do HS-5/HS-6, then D1→D3.

## Naming / trademarks

Keep clean-room/trademark hygiene: app + installer names use **Neuron** only — no
Element/ESS/Matrix-org marks in product names, bundle identifiers, or icons.
