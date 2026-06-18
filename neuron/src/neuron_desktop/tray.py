# SPDX-License-Identifier: Apache-2.0
"""The menu-bar / system-tray control app (D2).

The control logic — what the menu items do — lives in :class:`TrayController` and
:func:`menu_items`, which are GUI-agnostic and unit-tested. The actual tray icon is
drawn by ``pystray`` (imported lazily in :func:`run_tray`), so a headless
environment without a display can still import and test everything else.
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from neuron_desktop.config import DesktopConfig
from neuron_desktop.process import ServerProcess


def open_data_folder(path: Path) -> None:
    """Reveal a folder in the OS file manager (best effort, per platform)."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class TrayController:
    """The actions behind the tray menu, independent of any GUI toolkit."""

    def __init__(
        self,
        config: DesktopConfig,
        *,
        server: ServerProcess | None = None,
        console_opener: Callable[[str], object] = webbrowser.open,
        folder_opener: Callable[[Path], None] = open_data_folder,
    ) -> None:
        self._config = config
        self._server = server or ServerProcess(config)
        self._console_opener = console_opener
        self._folder_opener = folder_opener

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def toggle(self) -> None:
        if self._server.is_running():
            self._server.stop()
        else:
            self._server.start()

    def is_running(self) -> bool:
        return self._server.is_running()

    def status_text(self) -> str:
        return f"Server: {self._server.status()}"

    def toggle_text(self) -> str:
        return "Stop server" if self._server.is_running() else "Start server"

    def open_console(self) -> None:
        self._console_opener(self._config.console_url())

    def open_data_folder(self) -> None:
        self._folder_opener(self._config.data_path)

    def quit(self) -> None:
        self._server.stop()


@dataclass
class TrayItem:
    """One menu entry: ``text`` may be a string or a callable for dynamic labels."""

    text: str | Callable[[], str]
    action: Callable[[], None] | None
    enabled: bool = True


def menu_items(controller: TrayController, *, on_quit: Callable[[], None]) -> list[TrayItem]:
    """The tray menu as plain data (so it can be asserted on in tests)."""
    return [
        TrayItem(controller.toggle_text, controller.toggle),
        TrayItem(controller.status_text, None, enabled=False),
        TrayItem("Open console", controller.open_console),
        TrayItem("Open data folder", controller.open_data_folder),
        TrayItem("Quit", on_quit),
    ]


def _icon_image() -> object:
    """A simple tray icon (a filled circle) drawn with Pillow."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill=(63, 81, 181, 255))
    return image


def run_tray(config: DesktopConfig, *, autostart: bool = True) -> None:
    """Launch the tray app (requires ``pystray`` and a desktop session)."""
    try:
        import pystray
    except ImportError as exc:  # pragma: no cover - exercised only with the GUI extra
        raise SystemExit(
            "The tray app needs the GUI extras: pip install 'neuron[desktop-gui]'"
        ) from exc

    controller = TrayController(config)
    if autostart:
        controller.start()

    icon = pystray.Icon("Neuron", _icon_image(), "Neuron")

    def _quit() -> None:
        controller.quit()
        icon.stop()

    def _to_item(item: TrayItem) -> pystray.MenuItem:
        action = item.action
        return pystray.MenuItem(
            item.text,
            (lambda _icon, _item: action()) if action is not None else None,
            enabled=item.enabled,
        )

    icon.menu = pystray.Menu(*(_to_item(item) for item in menu_items(controller, on_quit=_quit)))
    icon.run()
