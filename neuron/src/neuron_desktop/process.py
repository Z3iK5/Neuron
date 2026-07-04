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
from typing import Protocol

from neuron_desktop import paths
from neuron_desktop.config import DesktopConfig


def default_server_command() -> list[str]:
    """How to launch the homeserver child process.

    In a normal install we run ``python -m neuron_server``. In a PyInstaller
    bundle there is no separate interpreter — ``sys.executable`` is the frozen app
    — so we re-exec the app itself with the internal ``_server`` command (handled
    in :mod:`neuron_desktop.cli`).
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "_server"]
    return [sys.executable, "-m", "neuron_server"]


def config_to_env(config: DesktopConfig) -> dict[str, str]:
    """The ``NEURON_SERVER_*`` environment that reproduces this config's settings."""
    settings = config.to_server_settings()
    return {
        "NEURON_SERVER_NAME": settings.name,
        "NEURON_SERVER_PUBLIC_BASE_URL": settings.public_base_url,
        "NEURON_SERVER_DATABASE_URL": settings.database_url,
        # PostgreSQL connection-pool size (ignored by SQLite); raised for medium/large
        # deployments. Without this the child server defaults to 1.
        "NEURON_SERVER_DB_POOL_SIZE": str(settings.db_pool_size),
        "NEURON_SERVER_MEDIA_STORE_PATH": settings.media_store_path,
        "NEURON_SERVER_SIGNING_KEY_PATH": settings.signing_key_path,
        "NEURON_SERVER_ADMIN_USERS": settings.admin_users,
        # Without this, the child server runs with first_user_admin=False, so the
        # first account created at /get-started never becomes an admin and can't
        # sign in to the console.
        "NEURON_SERVER_FIRST_USER_ADMIN": str(settings.first_user_admin),
        "NEURON_SERVER_REGISTRATION_ENABLED": str(settings.registration_enabled),
        "NEURON_SERVER_RATE_LIMIT_ENABLED": str(settings.rate_limit_enabled),
        "NEURON_SERVER_METRICS_ENABLED": str(settings.metrics_enabled),
        "NEURON_SERVER_STATE_RES_V2": str(settings.state_res_v2),
        "NEURON_SERVER_MAX_UPLOAD_BYTES": str(settings.max_upload_bytes),
        "NEURON_SERVER_LOG_LEVEL": settings.log_level,
        "NEURON_SERVER_BIND_HOST": settings.bind_host,
        "NEURON_SERVER_BIND_PORT": str(settings.bind_port),
        # Lets the in-process console settings page edit the persisted config.
        # Must match NeuronServerSettings.desktop_config_path -> NEURON_SERVER_DESKTOP_CONFIG_PATH.
        "NEURON_SERVER_DESKTOP_CONFIG_PATH": str(paths.config_path(config.data_path)),
    }


class _Process(Protocol):
    """The subset of ``subprocess.Popen`` the supervisor relies on."""

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
        self._command = command or default_server_command()
        self._popen = popen
        self._process: _Process | None = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def status(self) -> str:
        return "running" if self.is_running() else "stopped"

    def start(self) -> None:
        """Start the server if it isn't already running."""
        if self.is_running():
            return
        env = {**os.environ, **config_to_env(self._config)}
        self._process = self._popen(self._command, env=env)

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
