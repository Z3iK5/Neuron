# SPDX-License-Identifier: Apache-2.0
"""Tests for the D2 background-process supervisor and tray control logic.

The tray *icon* needs a real desktop session (so ``run_tray`` is not exercised
here), but the process management and the menu actions are pure logic and fully
tested — including a real child process started and stopped.
"""

from __future__ import annotations

import sys
from pathlib import Path

from neuron_desktop.config import DesktopConfig
from neuron_desktop.process import ServerProcess, config_to_env
from neuron_desktop.tray import TrayController, menu_items


def _config(tmp_path: Path) -> DesktopConfig:
    return DesktopConfig("hs.test", str(tmp_path), "admin", bind_port=8123)


def test_config_to_env_maps_settings(tmp_path: Path) -> None:
    env = config_to_env(_config(tmp_path))
    assert env["NEURON_SERVER_NAME"] == "hs.test"
    assert env["NEURON_SERVER_ADMIN_USERS"] == "admin"
    assert env["NEURON_SERVER_BIND_PORT"] == "8123"
    assert str(tmp_path / "homeserver.db") in env["NEURON_SERVER_DATABASE_URL"]


class _FakePopen:
    """A stand-in for subprocess.Popen with a controllable lifecycle."""

    instances: list[_FakePopen] = []

    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = command
        self.env = kwargs.get("env")
        self.pid = 4242
        self._returncode: int | None = None
        self.terminated = False
        _FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 0

    def kill(self) -> None:
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode or 0


def test_server_process_state_machine(tmp_path: Path) -> None:
    _FakePopen.instances.clear()
    server = ServerProcess(_config(tmp_path), command=["fake"], popen=_FakePopen)

    assert server.status() == "stopped" and not server.is_running()

    server.start()
    assert server.is_running() and server.status() == "running"
    assert server.pid == 4242
    # The child was launched with the server environment.
    assert _FakePopen.instances[-1].env["NEURON_SERVER_NAME"] == "hs.test"  # type: ignore[index]

    # Starting again while running does not spawn a second process.
    server.start()
    assert len(_FakePopen.instances) == 1

    server.stop()
    assert not server.is_running() and server.status() == "stopped"
    assert _FakePopen.instances[0].terminated

    # Stopping again is a no-op.
    server.stop()


def test_server_process_starts_and_stops_a_real_child(tmp_path: Path) -> None:
    # A trivial long-lived child (not the real server) exercises real start/stop.
    server = ServerProcess(
        _config(tmp_path), command=[sys.executable, "-c", "import time; time.sleep(60)"]
    )
    try:
        server.start()
        assert server.is_running()
        assert isinstance(server.pid, int)
    finally:
        server.stop(timeout=5)
    assert not server.is_running()


class _FakeServer:
    def __init__(self) -> None:
        self._running = False
        self.starts = 0

    def start(self) -> None:
        self._running = True
        self.starts += 1

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def status(self) -> str:
        return "running" if self._running else "stopped"


def test_tray_controller_actions(tmp_path: Path) -> None:
    opened: list[str] = []
    folders: list[Path] = []
    fake = _FakeServer()
    controller = TrayController(
        _config(tmp_path),
        server=fake,  # type: ignore[arg-type]
        console_opener=lambda url: opened.append(url),
        folder_opener=lambda path: folders.append(path),
    )

    assert controller.toggle_text() == "Start server"
    controller.toggle()
    assert controller.is_running() and controller.toggle_text() == "Stop server"
    assert controller.status_text() == "Server: running"
    controller.toggle()
    assert not controller.is_running()

    controller.open_console()
    assert opened == ["http://localhost:8123"]
    controller.open_data_folder()
    assert folders == [Path(str(tmp_path))]


def test_menu_items_wire_to_controller(tmp_path: Path) -> None:
    quit_called: list[bool] = []
    fake = _FakeServer()
    controller = TrayController(
        _config(tmp_path), server=fake, console_opener=lambda url: None  # type: ignore[arg-type]
    )
    items = menu_items(controller, on_quit=lambda: quit_called.append(True))

    labels = [item.text() if callable(item.text) else item.text for item in items]
    assert labels == ["Start server", "Server: stopped", "Open console", "Open data folder", "Quit"]

    # The status item is a non-actionable label.
    status_item = items[1]
    assert status_item.action is None and status_item.enabled is False

    # Activating the first item starts the server (toggle).
    items[0].action()  # type: ignore[misc]
    assert fake.is_running()

    # The quit item invokes the provided callback.
    items[-1].action()  # type: ignore[misc]
    assert quit_called == [True]
