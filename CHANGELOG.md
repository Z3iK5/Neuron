# Changelog

All notable changes to Neuron. Each release attaches desktop installers — macOS
`.dmg`, Windows `.exe`, Linux `.AppImage` — on the [Releases](https://github.com/Z3iK5/Neuron/releases) page.

## [0.0.3] — unreleased

### Changed
- **Desktop first run now lets you set your own password.** Instead of creating a
  default admin with a generated password, the app opens the browser to the in-app
  sign-up and makes the **first account you create the server administrator** (new
  `NEURON_SERVER_FIRST_USER_ADMIN` setting). `WELCOME.txt` points you at the sign-up
  link — no default password to change.

## [0.0.2] — 2026-06-18

### Fixed
- **Desktop app first run no longer crashes** with `input(): lost sys.stdin`. A
  double-clicked GUI app has no console, so first-run setup now runs
  non-interactively: it creates your admin account automatically and records the
  credentials in `WELCOME.txt` (which the app opens for you), instead of prompting
  on a terminal that isn't there.
- **Desktop app falls back to running the server** in the foreground if the
  tray/menu-bar backend can't start, instead of quitting silently.

## [0.0.1] — 2026-06-18

### Added
- **Matrix homeserver** (`neuron_server`): identity & auth, rooms (room v11),
  `GET /sync`, a media repository, E2EE key relay, the Client-Server API, a
  Synapse-compatible Admin API, and server-to-server federation.
- **Admin console** with shareable registration **invite links + QR codes**.
- **In-browser onboarding** (`/get-started`) and a **`neuron-server doctor`**
  preflight / health command.
- **Desktop app** with native installers for macOS, Windows, and Linux.
