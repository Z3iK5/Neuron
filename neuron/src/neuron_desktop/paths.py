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
