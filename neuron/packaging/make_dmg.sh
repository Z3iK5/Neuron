#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Package the PyInstaller-built macOS app (dist/Neuron.app) into an unsigned,
# drag-to-install .dmg. macOS only (uses the system `hdiutil`).
#
# Run from the `neuron/` directory, after:
#     pyinstaller --noconfirm packaging/neuron_desktop.spec
# Usage:
#     bash packaging/make_dmg.sh [output.dmg]   # default: Neuron-macos.dmg
#
# Signing / notarization are follow-ups (see docs/desktop.md); an unsigned .dmg
# installs fine but Gatekeeper will warn on first launch.
set -euo pipefail

APP="dist/Neuron.app"
OUT="${1:-Neuron-macos.dmg}"

if [ ! -d "$APP" ]; then
  echo "make_dmg: '$APP' not found — build the bundle with PyInstaller first" >&2
  exit 1
fi

# Stage the app next to an /Applications shortcut so the .dmg opens to the
# familiar "drag Neuron into Applications" layout.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$OUT"
hdiutil create \
  -volname "Neuron" \
  -srcfolder "$STAGE" \
  -fs HFS+ \
  -format UDZO \
  -ov \
  "$OUT"

echo "make_dmg: wrote $OUT"
