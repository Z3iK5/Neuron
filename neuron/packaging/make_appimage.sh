#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Package the PyInstaller one-folder bundle (dist/Neuron) into a portable Linux
# AppImage — a single self-contained executable that runs across distros without
# installation.
#
# Run from the `neuron/` directory, after:
#     pyinstaller --noconfirm packaging/neuron_desktop.spec
# Usage:
#     bash packaging/make_appimage.sh [output.AppImage]   # default: Neuron-<arch>.AppImage
#
# `appimagetool` is taken from $APPIMAGETOOL, then $PATH, else downloaded (its
# continuous release) and cached. Everything runs with APPIMAGE_EXTRACT_AND_RUN=1
# so it works without FUSE (CI runners / containers). appimagetool bundles its own
# mksquashfs, so no system squashfs-tools are required.
set -euo pipefail

ARCH="${ARCH:-$(uname -m)}"
BUNDLE="dist/Neuron"
OUT="${1:-Neuron-${ARCH}.AppImage}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$BUNDLE/Neuron" ]; then
  echo "make_appimage: '$BUNDLE/Neuron' not found — build the bundle with PyInstaller first" >&2
  exit 1
fi

export ARCH APPIMAGE_EXTRACT_AND_RUN=1

# --- locate (or fetch) appimagetool ----------------------------------------
TOOL="${APPIMAGETOOL:-}"
if [ -z "$TOOL" ]; then
  if command -v appimagetool >/dev/null 2>&1; then
    TOOL="$(command -v appimagetool)"
  else
    CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/neuron"
    mkdir -p "$CACHE"
    TOOL="$CACHE/appimagetool-${ARCH}.AppImage"
    if [ ! -x "$TOOL" ]; then
      echo "make_appimage: downloading appimagetool…"
      curl -fsSL -o "$TOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
      chmod +x "$TOOL"
    fi
  fi
fi

# --- assemble the AppDir ---------------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
APPDIR="$WORK/Neuron.AppDir"
mkdir -p "$APPDIR/usr/bin" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# The whole one-folder bundle (the Neuron executable + its _internal/ payload).
cp -a "$BUNDLE/." "$APPDIR/usr/bin/"

# AppRun: the entry point AppImage runs — exec the bundled binary, passing args.
cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/Neuron" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# Desktop entry (required by appimagetool) + a copy in the standard location.
cat > "$APPDIR/neuron.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Neuron
Comment=Your private Matrix homeserver
Exec=Neuron
Icon=neuron
Categories=Network;Chat;InstantMessaging;
Terminal=false
DESKTOP
cp "$APPDIR/neuron.desktop" "$APPDIR/usr/share/applications/neuron.desktop"

# Icon: at the AppDir root (matching Icon=neuron), as the .DirIcon thumbnail, and
# in the hicolor theme path for desktop integration.
ICON_SRC="$HERE/icons/neuron.png"
cp "$ICON_SRC" "$APPDIR/neuron.png"
cp "$ICON_SRC" "$APPDIR/.DirIcon"
cp "$ICON_SRC" "$APPDIR/usr/share/icons/hicolor/256x256/apps/neuron.png"

# --- build the AppImage ----------------------------------------------------
rm -f "$OUT"
"$TOOL" "$APPDIR" "$OUT"
echo "make_appimage: wrote $OUT"
