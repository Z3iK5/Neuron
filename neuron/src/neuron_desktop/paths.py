# SPDX-License-Identifier: Apache-2.0
"""Per-user data directory and the paths of the files Neuron keeps in it.

All of the server's durable state — database, media, signing key, and the desktop
config — lives under one directory so it is easy to back up or remove:

* macOS   ``~/Library/Application Support/Neuron``
* Windows ``%LOCALAPPDATA%\\Neuron``
* Linux   ``~/.local/share/Neuron``

Override the whole location with the ``NEURON_DATA_DIR`` environment variable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import platformdirs

APP_NAME = "Neuron"
DATA_DIR_ENV = "NEURON_DATA_DIR"


def data_dir() -> Path:
    """The directory holding all Neuron state (honours ``NEURON_DATA_DIR``)."""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def config_path(base: Path) -> Path:
    return base / "config.json"


def database_path(base: Path) -> Path:
    return base / "homeserver.db"


def media_path(base: Path) -> Path:
    return base / "media"


def signing_key_path(base: Path) -> Path:
    return base / "signing.key"


def reveal(path: Path) -> None:
    """Open a file or folder in the OS default handler (best effort, never raises)."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def version_path(base: Path) -> Path:
    """File recording the app version that last configured/ran this data dir.

    Used to tell an upgrade (a different version over existing data) from a normal
    relaunch (same version) and from a clean first run (no data at all).
    """
    return base / ".neuron-version"
