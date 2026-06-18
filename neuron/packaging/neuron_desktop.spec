# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Neuron Desktop app (D3).
#
# Build (from the `neuron/` directory, after `pip install -e ".[desktop-gui]"`):
#     pyinstaller --noconfirm packaging/neuron_desktop.spec
#
# Produces a one-folder bundle in `dist/Neuron/`. The CI workflow
# (.github/workflows/desktop-installers.yml) builds this on macOS, Windows and
# Linux and uploads the zipped bundle as a release artifact. Wrapping the bundle
# in native installer formats (.dmg / .msi / AppImage) is a follow-up.

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

_entry = os.path.join(SPECPATH, "app_entry.py")  # noqa: F821 (SPECPATH injected by PyInstaller)

# Dynamic imports PyInstaller's static analysis would otherwise miss: uvicorn
# loads its protocol/loop implementations by name, and our packages wire routers.
hidden = set()
for package in ("uvicorn", "neuron_server", "neuron_desktop"):
    hidden |= set(collect_submodules(package))
# PyNaCl reaches its Ed25519 code through cffi's C extension, which the analysis
# does not see; aiosqlite/asyncpg are imported by name by the storage layer.
hidden |= {"aiosqlite", "asyncpg", "nacl", "platformdirs", "cffi", "_cffi_backend"}

# Packages that ship data files / dynamically-loaded backends. ``pystray`` selects
# a platform backend on import, which can fail on a headless builder; skip it
# gracefully so the (non-GUI) server bundle still builds there.
datas: list = []
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
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Neuron",
    console=False,  # tray app: no terminal window
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Neuron")
