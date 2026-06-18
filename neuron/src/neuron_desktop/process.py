# SPDX-License-Identifier: Apache-2.0
"""Supervise the homeserver as a managed background process (D2).

The tray app needs to start and stop the server without blocking its own event
loop, so the server runs as a child process (``python -m neuron_server``) with its
settings passed through the environment. The ``Popen`` factory is injectable so the
start/stop/status state machine can be tested without launching a real server.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any, Protocol

from neuron_desktop.config import DesktopConfig


def config_to_env(config: DesktopConfig) -> dict[str, str]:
    """The ``NEURON_SERVER_*`` environment that reproduces this config's settings."""
    settings = config.to_server_settings()
    return {
        "NEURON_SERVER_NAME": settings.name,
        "NEURON_SERVER_PUBLIC_BASE_URL": settings.public_base_url,
        "NEURON_SERVER_DATABASE_URL": settings.database_url,
        "NEURON_SERVER_MEDIA_STORE_PATH": settings.media_store_path,
        "NEURON_SERVER_SIGNING_KEY_PATH": settings.signing_key_path,
        "NEURON_SERVER_ADMIN_USERS": settings.admin_users,
        "NEURON_SERVER_BIND_HOST": settings.bind_host,
        "NEURON_SERVER_BIND_PORT": str(settings.bind_port),
    }


class _Process(Protocol):
    """The subset of ``subprocess.Popen`` the supervisor relies on."""

    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


PopenFactory = Callable[..., _Process]


class ServerProcess:
    """Starts, stops and reports on the homeserver child process."""

    def __init__(
        self,
        config: DesktopConfig,
        *,
        command: list[str] | None = None,
        popen: PopenFactory = subprocess.Popen,
    ) -> None:
        self._config = config
        self._command = command or [sys.executable, "-m", "neuron_server"]
        self._popen = popen
        self._process: _Process | None = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def status(self) -> str:
        return "running" if self.is_running() else "stopped"

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def start(self) -> None:
        """Start the server if it isn't already running."""
        if self.is_running():
            return
        env = {**os.environ, **config_to_env(self._config)}
        kwargs: dict[str, Any] = {"env": env}
        self._process = self._popen(self._command, **kwargs)

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the server, escalating to kill if it doesn't exit in ``timeout``."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
