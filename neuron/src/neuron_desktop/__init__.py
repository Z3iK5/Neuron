# SPDX-License-Identifier: Apache-2.0
"""Neuron Desktop — run your own homeserver as an installed app.

This package is a thin supervisor around ``neuron_server``: it resolves a per-user
data directory, runs a one-time first-run setup (server name + admin account), and
starts/stops the server, reusing the existing web admin console as the UI. The
tray/menu-bar front-end (D2) and native installers (D3) build on this logic layer.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

__all__ = ["__version__"]

# Single source of truth: the installed package metadata (pyproject version).
try:
    __version__ = _version("neuron")
except PackageNotFoundError:  # pragma: no cover - metadata present when installed
    __version__ = "0.0.0"
