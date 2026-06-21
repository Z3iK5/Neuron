# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Neuron Desktop app (D3).
#
# Build (from the `neuron/` directory, after `pip install -e ".[desktop-gui]"`):
#     pyinstaller --noconfirm packaging/neuron_desktop.spec
#
# Produces a one-folder bundle in `dist/Neuron/`. On macOS it additionally wraps
# that bundle into a proper `dist/Neuron.app` (so it can be packaged as a `.dmg`
# by packaging/make_dmg.sh). The CI workflow (.github/workflows/desktop-installers.yml)
# builds this on macOS, Windows and Linux. Code signing / notarization are follow-ups
# (see docs/desktop.md).

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

_entry = os.path.join(SPECPATH, "app_entry.py")  # noqa: F821 (SPECPATH injected by PyInstaller)

# Dynamic imports PyInstaller's static analysis would otherwise miss: uvicorn
# loads its protocol/loop implementations by name, and our packages wire routers.
hidden = set()
for package in ("uvicorn", "neuron_server", "neuron_desktop"):
    hidden |= set(collect_submodules(package))
# Starlette imports python-multipart lazily (only inside Request.form()), so the
# static analysis misses it and the onboarding form (POST /get-started) would
# 500 with "python-multipart must be installed". Force it (both the modern
# ``python_multipart`` and legacy ``multipart`` import names) into the bundle.
# The admin console also reaches segno lazily (inside the invite-QR route) and
# itsdangerous via Starlette's SessionMiddleware; pull both in too.
# The console also reaches webauthn (passkeys) lazily inside its routes.
for package in ("python_multipart", "multipart", "segno", "itsdangerous", "webauthn", "cbor2"):
    try:
        hidden |= set(collect_submodules(package))
    except Exception as exc:  # noqa: BLE001 - one import alias may be absent
        print(f"[neuron spec] could not collect {package!r}: {exc}")
# PyNaCl reaches its Ed25519 code through cffi's C extension, which the analysis
# does not see; aiosqlite/asyncpg are imported by name by the storage layer.
hidden |= {"aiosqlite", "asyncpg", "nacl", "platformdirs", "cffi", "_cffi_backend"}
hidden |= {"python_multipart", "multipart", "segno", "itsdangerous", "webauthn", "cbor2"}

# Packages that ship data files / dynamically-loaded backends. ``pystray`` selects
# a platform backend on import, which can fail on a headless builder; skip it
# gracefully so the (non-GUI) server bundle still builds there.
# Ship the package's dist metadata so importlib.metadata.version("neuron") works
# in the frozen app (the console's server-version display reads it).
datas: list = []
try:
    datas += copy_metadata("neuron")
except Exception as exc:  # noqa: BLE001 - metadata always present in a real build
    print(f"[neuron spec] could not copy neuron metadata: {exc}")
binaries: list = []
for package in ("pystray", "PIL"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception as exc:  # noqa: BLE001 - optional GUI backend may be absent
        print(f"[neuron spec] skipping optional package {package!r}: {exc}")
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hidden |= set(pkg_hidden)

a = Analysis(
    [_entry],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(hidden),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],  # tkinter IS needed now: the native pre-start settings window uses it
    noarchive=False,
)
pyz = PYZ(a.pure)

# OS-level app icon. Windows/Linux use the generated .ico; macOS wants an .icns
# (generated in CI via iconutil when available), so fall back gracefully.
import sys  # noqa: E402

_icns = os.path.join(SPECPATH, "icons", "neuron.icns")  # noqa: F821
_ico = os.path.join(SPECPATH, "icons", "neuron.ico")  # noqa: F821
_icon = (_icns if os.path.exists(_icns) else None) if sys.platform == "darwin" else _ico

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Neuron",
    console=False,  # tray app: no terminal window
    disable_windowed_traceback=False,
    icon=_icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Neuron")

# On macOS, wrap the one-folder bundle into a real `.app` so it can be dragged to
# /Applications and packaged as a `.dmg`. (Windows/Linux keep the plain folder.)
# Keep the version in step with the project version in pyproject.toml.
if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821 (BUNDLE injected by PyInstaller)
        coll,
        name="Neuron.app",
        icon=_icns if os.path.exists(_icns) else None,
        bundle_identifier="org.neuron.desktop",
        version="0.0.17",
        info_plist={
            "CFBundleName": "Neuron",
            "CFBundleDisplayName": "Neuron",
            "CFBundleShortVersionString": "0.0.17",
            "CFBundleVersion": "0.0.17",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
