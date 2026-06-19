# Neuron Desktop

Run your own Matrix homeserver as an installed application — no terminal, no config
files. Neuron Desktop wraps `neuron_server` with a first-run wizard, a per-user data
directory, and a menu-bar/tray control.

## Install & first run

```bash
pipx install "neuron[desktop]"   # or: pip install -e ".[desktop]"
neuron-desktop
```

On first launch the setup wizard asks for a server name and creates your admin account,
then starts the homeserver. Useful subcommands:

```bash
neuron-desktop            # first-run setup if needed, then run the server
neuron-desktop setup      # (re)run the first-run wizard only
neuron-desktop where      # print the data directory and config path
neuron-desktop console    # open the admin console in a browser
neuron-desktop tray       # run the menu-bar / system-tray app (needs a desktop session)
```

## Admin console

The homeserver serves a built-in web admin console at **`/console`** (e.g.
`http://localhost:8008/console`) — `neuron-desktop console` and the tray's *Open console*
open it for you. Sign in with the **admin account you created** at first run (the same Matrix
username + password); the console runs in the same app and talks to the server in-process, so
there is no separate password or token.

From it you can see a server **overview**, manage **users** (create, reset password,
deactivate, grant/revoke admin), inspect **rooms** (members + state), and create **invite**
links (with QR codes) so other people can sign up. Moderation actions (shadow-ban, server
notices, room block/delete, redaction, content reports) are shown as "coming soon" and will be
enabled as the homeserver gains the backing for them.

## Settings & server name

Your **server name** (e.g. `chat.example.org`) is the homeserver's permanent identity — it's
built into every account, room and message, so it **cannot be changed once the server starts**.
On first run a small **Settings window** lets you choose it (it defaults to your computer's
name); you can reopen that window any time from the tray's **Settings…** item to adjust
pre-start options. Click the server name next to **Sign out** in the console to open
**Server settings**, which shows the name (read-only), runs the **doctor** health checks, and
lets you toggle open registration. Changes apply after a restart — use the tray's **Restart
server** item.

## Where your data lives

All state — database, media, the federation signing key, and `config.json` — is kept in
the OS application-data directory:

| OS | Location |
|----|----------|
| macOS | `~/Library/Application Support/Neuron` |
| Windows | `%LOCALAPPDATA%\Neuron` |
| Linux | `~/.local/share/Neuron` |

Override it with the `NEURON_DATA_DIR` environment variable. Back up this directory (the
signing key especially) to preserve your server's identity.

## Native installers

Each release builds a native installer per platform in CI
(`.github/workflows/desktop-installers.yml`):

| Platform | Installer | Notes |
|----------|-----------|-------|
| macOS | `Neuron.app` packaged as a `.dmg` | drag to Applications |
| Windows | `Neuron-Setup-x64.exe` (Inno Setup) | per-user install, no admin/UAC prompt |
| Linux | `Neuron-x86_64.AppImage` | single portable file, no install needed |

### macOS code signing & notarization

The macOS build is **signed with a Developer ID and notarized** automatically when
these repository secrets are present (otherwise it builds unsigned, and Gatekeeper
warns on first launch — right-click → Open):

| Secret | What it is |
|--------|-----------|
| `MACOS_CERT_P12` | Your *Developer ID Application* cert exported as a `.p12` (with the private key), base64-encoded |
| `MACOS_CERT_PASSWORD` | The `.p12` export password |
| `APPLE_API_KEY_ID` | App Store Connect API key id (Users & Access → Integrations) |
| `APPLE_API_ISSUER_ID` | The issuer id on that page |
| `APPLE_API_KEY_P8` | The downloaded `AuthKey_*.p8`, base64-encoded |

The signing identity is auto-detected from the cert; the hardened-runtime
entitlements live in `packaging/entitlements.plist`. Signing the bundled Python app
for notarization can need a round of iteration — `notarytool` prints a detailed log
on rejection.

> **Windows / Linux** `.exe`/`.AppImage` installers are unsigned (SmartScreen may warn).

### Windows MSIX (Microsoft Store)

Every build also produces a **`.msix`** package, uploaded as a workflow **artifact**
(`neuron-windows-msix`) — it is *not* attached to the GitHub Release. With no
configuration it is **self-signed** for sideload testing: download the artifact, import
the bundled `Neuron-dev-cert.cer` into *Trusted People*, then install the `.msix`.

To make it **Store-ready**, set these repository *variables* (Settings → Secrets and
variables → Actions → Variables) from your Partner Center app reservation — the package
is then built with that identity and left unsigned for Store submission:

| Variable | From Partner Center |
|----------|---------------------|
| `MSIX_IDENTITY_NAME` | Package/Identity **Name** |
| `MSIX_PUBLISHER` | Package/Identity **Publisher** (`CN=…`) |
| `MSIX_PUBLISHER_DISPLAY_NAME` | Publisher display name |

### Building locally

From the `neuron/` directory, with the desktop extra and PyInstaller installed
(`pip install -e ".[desktop-gui]" pyinstaller`):

```bash
pyinstaller --noconfirm packaging/neuron_desktop.spec   # -> dist/Neuron/ (and dist/Neuron.app on macOS)

# then wrap it for your platform:
bash   packaging/make_appimage.sh     # Linux  -> Neuron-x86_64.AppImage
bash   packaging/make_dmg.sh          # macOS  -> Neuron-macos.dmg
# Windows: compile packaging/neuron.iss with Inno Setup (ISCC.exe) -> Neuron-Setup-x64.exe
```

PyInstaller can't cross-compile, so each installer must be built on its own OS (which is
why CI uses a macOS, Windows, and Linux runner). The app icon is generated from the
NEURON brand mark — `packaging/make_icns.py` builds the macOS `.icns`, and
`packaging/make_brand_assets.py` regenerates the shared icon/asset set.

## Publishing a release

Pushing a `v*` tag runs the installer workflow and attaches all three installers to the
GitHub Release automatically.
