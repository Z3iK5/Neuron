# SPDX-License-Identifier: Apache-2.0
"""Running the server and opening the console.

D1 runs the server in the foreground (``uvicorn.run``); the tray app (D2) will
move it to a managed background process. ``open_console`` takes an injectable
opener so it can be tested without launching a browser.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable

from neuron_core import configure_logging
from neuron_desktop.config import DesktopConfig

Opener = Callable[[str], bool]


def open_console(config: DesktopConfig, *, opener: Opener = webbrowser.open) -> str:
    """Open the admin console in a browser; return the URL that was opened."""
    url = config.console_url()
    opener(url)
    return url


def serve(config: DesktopConfig) -> None:
    """Run the homeserver in the foreground using this config's settings."""
    import uvicorn

    from neuron_server.app import create_app

    settings = config.to_server_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    app = create_app(settings)
    # We already installed our own logging above. Pass ``log_config=None`` so
    # uvicorn does not apply its default logging config, whose colourised
    # formatter calls ``sys.stdout.isatty()`` and crashes when stdout is None in
    # a windowed frozen build (PyInstaller ``console=False``).
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port, log_config=None)
