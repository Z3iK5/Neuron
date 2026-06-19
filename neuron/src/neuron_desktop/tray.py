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
        self._console_opener(self._config.admin_console_url())

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


def adapt_menu_item(item: TrayItem, menu_item_cls: Callable[..., object]) -> object:
    """Adapt a GUI-agnostic :class:`TrayItem` to a pystray ``MenuItem``.

    pystray invokes a *callable* menu label with the ``MenuItem`` as a positional
    argument (its ``MenuItem.text`` property does ``self._text(self)``), and an
    action as ``action(icon, item)``. Our controller exposes zero-argument methods
    (e.g. ``toggle_text``), so passing them through unwrapped makes pystray call
    them with an extra argument -> ``TypeError: ... takes 1 positional argument but
    2 were given`` the instant the menu is built (the crash seen on Windows and
    macOS). Wrap both callables so they accept and ignore the extra argument(s).

    ``menu_item_cls`` is ``pystray.MenuItem`` in production; tests pass a stub to
    assert the wrapped label is callable with the item argument.
    """
    text = item.text
    action = item.action
    return menu_item_cls(
        (lambda _item: text()) if callable(text) else text,
        (lambda _icon, _item: action()) if action is not None else None,
        enabled=item.enabled,
    )


def _icon_image() -> object:
    """The NEURON app icon (Neural Shield mark on a navy squircle)."""
    from neuron_desktop.icon import render_icon

    return render_icon(64)


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

    def _quit() -> None:
        controller.quit()
        icon.stop()

    try:
        icon = pystray.Icon("Neuron", _icon_image(), "Neuron")
        icon.menu = pystray.Menu(
            *(
                adapt_menu_item(item, pystray.MenuItem)
                for item in menu_items(controller, on_quit=_quit)
            )
        )
        icon.run()
    except BaseException:
        # If the tray backend fails *after* we autostarted the homeserver child,
        # stop it so any foreground fallback (see cli.py) can bind the port
        # cleanly instead of two servers racing for it (which hangs the GUI).
        if autostart:
            controller.stop()
        raise
