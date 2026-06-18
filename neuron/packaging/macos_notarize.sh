#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Submit a signed artifact (.dmg or .app zip) to Apple notarization and staple the
# ticket, using an App Store Connect API key. macOS only (uses xcrun notarytool).
#
# Requires in the environment (base64 where noted):
#   APPLE_API_KEY_ID     — the App Store Connect API key id
#   APPLE_API_ISSUER_ID  — the issuer id
#   APPLE_API_KEY_P8     — the AuthKey_XXXX.p8 contents, base64-encoded
# Usage:  bash packaging/macos_notarize.sh Neuron-macos.dmg
set -euo pipefail

TARGET="${1:?usage: macos_notarize.sh <path-to-dmg-or-zip>}"
KEY_FILE="${RUNNER_TEMP:-/tmp}/asc_api_key.p8"
trap 'rm -f "$KEY_FILE"' EXIT

printf '%s' "$APPLE_API_KEY_P8" | base64 --decode > "$KEY_FILE"

xcrun notarytool submit "$TARGET" \
  --key "$KEY_FILE" \
  --key-id "$APPLE_API_KEY_ID" \
  --issuer "$APPLE_API_ISSUER_ID" \
  --wait

# Staple the ticket so the artifact validates offline (Gatekeeper).
xcrun stapler staple "$TARGET"
xcrun stapler validate "$TARGET"
echo "macos_notarize: notarized + stapled $TARGET"
