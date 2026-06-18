#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Codesign a PyInstaller .app for Developer ID distribution under the hardened
# runtime (a prerequisite for Apple notarization). Signs every nested Mach-O
# binary from the inside out, then the bundle itself.
#
# Requires in the environment:
#   MACOS_IDENTITY  — the signing identity (a Developer ID Application hash/name)
#   MACOS_KEYCHAIN  — the keychain holding the identity
# Usage:  bash packaging/macos_sign.sh [dist/Neuron.app]
set -euo pipefail

APP="${1:-dist/Neuron.app}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENTITLEMENTS="$HERE/entitlements.plist"

if [ ! -d "$APP" ]; then
  echo "macos_sign: '$APP' not found" >&2
  exit 1
fi

sign() {
  codesign --force --timestamp --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$MACOS_IDENTITY" --keychain "$MACOS_KEYCHAIN" "$1"
}

# 1. Nested libraries (.dylib / .so) — sign deepest first.
while IFS= read -r -d '' lib; do
  sign "$lib"
done < <(find "$APP/Contents" -type f \( -name '*.dylib' -o -name '*.so' \) -print0)

# 2. Nested executables (the bundled Python interpreter, helper binaries, the
#    main launcher) — anything with the executable bit under MacOS/.
while IFS= read -r -d '' exe; do
  sign "$exe"
done < <(find "$APP/Contents/MacOS" -type f -perm +111 -print0)

# 3. The .app bundle as a whole.
sign "$APP"

codesign --verify --strict --verbose=2 "$APP"
echo "macos_sign: signed $APP with $MACOS_IDENTITY"
