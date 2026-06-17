# SPDX-License-Identifier: Apache-2.0
"""Run the homeserver: ``python -m neuron_server``.

Loads settings from the environment (``NEURON_SERVER_*``), builds the app, and
serves it with uvicorn on the configured host/port.
"""

from __future__ import annotations

import uvicorn

from neuron_core import configure_logging
from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings


def main() -> None:
    settings = NeuronServerSettings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    app = create_app(settings)
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
