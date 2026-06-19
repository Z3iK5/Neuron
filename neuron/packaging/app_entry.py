# SPDX-License-Identifier: Apache-2.0
"""PyInstaller entry point for the bundled Neuron Desktop app.

Double-clicking the bundled app (no arguments) launches the tray control app; any
explicit arguments are passed through to the normal ``neuron-desktop`` CLI — which
includes the internal ``_server`` command the app uses to run the homeserver as a
child process.
"""

from __future__ import annotations

import io
import os
import sys


def _ensure_std_streams() -> None:
    """Guarantee ``sys.stdout`` / ``sys.stderr`` are real, writable streams.

    A windowed PyInstaller build (``console=False``) on Windows has no console, so
    ``sys.stdout`` and ``sys.stderr`` are ``None``. Any code that writes to them —
    or merely probes ``sys.stdout.isatty()``, as uvicorn's default logging config
    does — then crashes (``AttributeError: 'NoneType' object has no attribute
    'isatty'``). Point the missing streams at a log file under the per-user data
    directory (falling back to ``os.devnull``) so logs are captured and nothing
    can crash on a missing stream. Runs before any other import that might log.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return

    replacement: object = None
    try:
        from neuron_desktop import paths

        base = paths.data_dir()
        base.mkdir(parents=True, exist_ok=True)
        replacement = open(base / "neuron.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        try:
            replacement = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            replacement = io.StringIO()

    if sys.stdout is None:
        sys.stdout = replacement  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = replacement  # type: ignore[assignment]


if __name__ == "__main__":
    # Repair the standard streams *first*, before importing anything that logs.
    _ensure_std_streams()

    import multiprocessing

    # Required so the frozen app can safely spawn child processes on Windows/macOS.
    multiprocessing.freeze_support()

    from neuron_desktop.cli import main

    argv = sys.argv[1:] or ["tray"]
    raise SystemExit(main(argv))
