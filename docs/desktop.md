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

> Installers are currently **unsigned**. macOS Gatekeeper and Windows SmartScreen will
> warn on first launch; on macOS, right-click → Open the first time. Code signing and
> notarization are a planned follow-up.

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
