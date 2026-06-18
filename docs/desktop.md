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

> **Windows / Linux** installers are still unsigned (SmartScreen may warn on Windows).
> A Windows MSIX for the Microsoft Store is built as a separate artifact.

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
