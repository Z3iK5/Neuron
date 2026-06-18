# SPDX-License-Identifier: Apache-2.0
"""PyInstaller entry point for the bundled Neuron Desktop app.

Double-clicking the bundled app (no arguments) launches the tray control app; any
explicit arguments are passed through to the normal ``neuron-desktop`` CLI — which
includes the internal ``_server`` command the app uses to run the homeserver as a
child process.
"""

from __future__ import annotations

import multiprocessing
import sys

from neuron_desktop.cli import main

if __name__ == "__main__":
    # Required so the frozen app can safely spawn child processes on Windows/macOS.
    multiprocessing.freeze_support()
    argv = sys.argv[1:] or ["tray"]
    raise SystemExit(main(argv))
