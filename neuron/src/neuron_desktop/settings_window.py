# SPDX-License-Identifier: Apache-2.0
"""A small native (tkinter) settings window for pre-start desktop configuration.

It edits the settings that must be chosen *before* the homeserver starts — above all
the **official server name** (the homeserver's permanent identity; see
``neuron_server.app._ensure_server_identity``). Used on first run (to name the server
instead of defaulting to the computer's hostname) and from the tray's *Settings…*
item, which runs it in a separate process via the ``neuron-desktop settings`` command.

tkinter is the Python standard-library GUI toolkit, so this adds no dependency. On a
headless machine ``tkinter``/``Tk()`` raises, so the public entry points catch that and
return ``None`` — callers then fall back to non-interactive defaults.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from neuron_desktop import paths
from neuron_desktop.config import DesktopConfig, validate_database_url

# Server-name rule (mirrors neuron_server.doctor._check_server_name): no spaces/slashes.
_INVALID_NAME_CHARS = (" ", "/")


def updated_config(
    config: DesktopConfig,
    *,
    server_name: str,
    bind_host: str,
    bind_port: str | int,
    database_url: str | None = None,
    db_pool_size: str | int | None = None,
) -> DesktopConfig:
    """Return ``config`` with the edited fields applied (empty/invalid inputs ignored).

    ``database_url``/``db_pool_size`` left as ``None`` keep the existing values (so
    callers that don't edit them are unaffected). A blank ``database_url`` selects
    the built-in SQLite backend.
    """
    name = (server_name or "").strip() or config.server_name
    host = (bind_host or "").strip() or config.bind_host
    try:
        port = int(str(bind_port).strip())
    except (TypeError, ValueError):
        port = config.bind_port
    db_url = config.database_url if database_url is None else (database_url or "").strip()
    if db_pool_size is None:
        pool = config.db_pool_size
    else:
        try:
            pool = max(1, int(str(db_pool_size).strip()))
        except (TypeError, ValueError):
            pool = config.db_pool_size
    return replace(
        config,
        server_name=name,
        bind_host=host,
        bind_port=port,
        database_url=db_url,
        db_pool_size=pool,
    )


def validate_server_name(name: str) -> str | None:
    """Return an error message for an invalid server name, or None if it's acceptable."""
    name = (name or "").strip()
    if not name:
        return "Server name is required."
    if any(ch in name for ch in _INVALID_NAME_CHARS):
        return "Server name cannot contain spaces or slashes."
    return None


def identity_committed(config: DesktopConfig) -> bool:
    """True if the homeserver has already initialized (its database file exists).

    Once that happens the server name is locked (changing it would stop the server
    booting), so the window shows it read-only.
    """
    return paths.database_path(config.data_path).exists()


def run_first_run_window(
    config: DesktopConfig,
    *,
    on_save: Callable[[DesktopConfig], str],
) -> None:
    """First-run wizard in one window: settings, then a 'getting started' panel.

    The settings panel collects the server name (etc.); on Continue it calls
    ``on_save(updated_config)`` — which persists the config and starts the server,
    returning the homeserver base URL — and the same window switches to a getting-
    started panel whose buttons open the browser to create an account or sign in.

    Raises (e.g. ``tkinter.TclError``) when no display is available; callers handle it.
    """
    import tkinter as tk
    import webbrowser
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Neuron — Set up your server")
    root.resizable(False, False)
    outer = ttk.Frame(root, padding=18)
    outer.grid(sticky="nsew")
    settings = ttk.Frame(outer)
    started = ttk.Frame(outer)
    settings.grid(column=0, row=0, sticky="nsew")

    ttk.Label(
        settings, text="Welcome to Neuron. Name your server before it starts.", wraplength=380
    ).grid(column=0, row=0, columnspan=2, pady=(0, 12))
    ttk.Label(settings, text="Server name").grid(column=0, row=1, sticky="w")
    name_var = tk.StringVar(value=config.server_name)
    name_entry = ttk.Entry(settings, textvariable=name_var, width=32)
    name_entry.grid(column=1, row=1, sticky="ew", pady=3)
    ttk.Label(
        settings,
        text="Permanent once the server starts — it's built into every account, room "
        "and message.",
        wraplength=380,
        foreground="#7C8896",
    ).grid(column=0, row=2, columnspan=2, sticky="w", pady=(0, 10))
    ttk.Label(settings, text="Bind host").grid(column=0, row=3, sticky="w")
    host_var = tk.StringVar(value=config.bind_host)
    ttk.Entry(settings, textvariable=host_var, width=32).grid(column=1, row=3, sticky="ew", pady=3)
    ttk.Label(settings, text="Bind port").grid(column=0, row=4, sticky="w")
    port_var = tk.StringVar(value=str(config.bind_port))
    ttk.Entry(settings, textvariable=port_var, width=32).grid(column=1, row=4, sticky="ew", pady=3)

    ttk.Label(settings, text="Database").grid(column=0, row=5, sticky="w")
    db_var = tk.StringVar(value=config.database_url)
    ttk.Entry(settings, textvariable=db_var, width=32).grid(column=1, row=5, sticky="ew", pady=3)
    ttk.Label(settings, text="Pool size").grid(column=0, row=6, sticky="w")
    pool_var = tk.StringVar(value=str(config.db_pool_size))
    ttk.Entry(settings, textvariable=pool_var, width=32).grid(column=1, row=6, sticky="ew", pady=3)
    ttk.Label(
        settings,
        text="Leave Database blank for the built-in SQLite (personal / small servers). For a "
        "medium / large deployment enter a PostgreSQL URL "
        "(postgresql://user:pass@host:5432/neuron) and optionally raise the pool size.",
        wraplength=380,
        foreground="#7C8896",
    ).grid(column=0, row=7, columnspan=2, sticky="w", pady=(0, 10))

    def _show_started(base_url: str) -> None:
        settings.grid_remove()
        for child in started.winfo_children():
            child.destroy()
        started.grid(column=0, row=0, sticky="nsew")
        ttk.Label(started, text="Your server is running.", wraplength=380).grid(
            column=0, row=0, pady=(0, 4)
        )
        ttk.Label(
            started,
            text=f"Create your account to begin, or open the console to sign in.\n\n{base_url}",
            wraplength=380,
            justify="center",
            foreground="#7C8896",
        ).grid(column=0, row=1, pady=(0, 14))
        base = base_url.rstrip("/")
        ttk.Button(
            started, text="Create an account",
            command=lambda: webbrowser.open(f"{base}/get-started"),
        ).grid(column=0, row=2, sticky="ew", pady=3)
        ttk.Button(
            started, text="Open console (sign in)",
            command=lambda: webbrowser.open(f"{base}/console"),
        ).grid(column=0, row=3, sticky="ew", pady=3)
        ttk.Button(started, text="Finish", command=root.destroy).grid(
            column=0, row=4, sticky="ew", pady=(10, 0)
        )

    def _continue() -> None:
        err = validate_server_name(name_var.get()) or validate_database_url(db_var.get())
        if err:
            messagebox.showerror("Invalid setting", err)
            return
        updated = updated_config(
            config,
            server_name=name_var.get(),
            bind_host=host_var.get(),
            bind_port=port_var.get(),
            database_url=db_var.get(),
            db_pool_size=pool_var.get(),
        )
        try:
            base_url = on_save(updated)
        except Exception as exc:  # noqa: BLE001 - surface start failures in the dialog
            messagebox.showerror("Could not start the server", str(exc))
            return
        _show_started(base_url)

    buttons = ttk.Frame(settings)
    buttons.grid(column=0, row=8, columnspan=2, pady=(14, 0), sticky="e")
    ttk.Button(buttons, text="Continue", command=_continue).grid(column=0, row=0)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    name_entry.focus_set()
    root.mainloop()


def open_settings_window(
    config: DesktopConfig, *, first_run: bool = False
) -> DesktopConfig | None:
    """Show the modal settings form; return an updated config on save, else ``None``.

    Raises (e.g. ``tkinter.TclError``) when no display is available; callers handle it.
    """
    import tkinter as tk
    from tkinter import messagebox, ttk

    name_locked = identity_committed(config) and not first_run

    root = tk.Tk()
    root.title("Neuron — Settings")
    root.resizable(False, False)
    result: dict[str, DesktopConfig | None] = {"value": None}

    frame = ttk.Frame(root, padding=18)
    frame.grid(sticky="nsew")

    intro = (
        "Welcome to Neuron. Name your server before it starts."
        if first_run
        else "Settings that apply when the server (re)starts."
    )
    ttk.Label(frame, text=intro, wraplength=380).grid(column=0, row=0, columnspan=2, pady=(0, 12))

    ttk.Label(frame, text="Server name").grid(column=0, row=1, sticky="w")
    name_var = tk.StringVar(value=config.server_name)
    name_entry = ttk.Entry(frame, textvariable=name_var, width=32)
    name_entry.grid(column=1, row=1, sticky="ew", pady=3)
    if name_locked:
        name_entry.state(["disabled"])
    note = (
        "Permanent once the server starts — it's built into every account, room and "
        "message."
        if not name_locked
        else "Locked — the server has already started under this name."
    )
    ttk.Label(frame, text=note, wraplength=380, foreground="#7C8896").grid(
        column=0, row=2, columnspan=2, sticky="w", pady=(0, 10)
    )

    ttk.Label(frame, text="Bind host").grid(column=0, row=3, sticky="w")
    host_var = tk.StringVar(value=config.bind_host)
    ttk.Entry(frame, textvariable=host_var, width=32).grid(column=1, row=3, sticky="ew", pady=3)

    ttk.Label(frame, text="Bind port").grid(column=0, row=4, sticky="w")
    port_var = tk.StringVar(value=str(config.bind_port))
    ttk.Entry(frame, textvariable=port_var, width=32).grid(column=1, row=4, sticky="ew", pady=3)

    # Database backend — locked once the server has started under a backend (its data
    # lives there), just like the server name.
    ttk.Label(frame, text="Database").grid(column=0, row=5, sticky="w")
    db_var = tk.StringVar(value=config.database_url)
    db_entry = ttk.Entry(frame, textvariable=db_var, width=32)
    db_entry.grid(column=1, row=5, sticky="ew", pady=3)
    ttk.Label(frame, text="Pool size").grid(column=0, row=6, sticky="w")
    pool_var = tk.StringVar(value=str(config.db_pool_size))
    pool_entry = ttk.Entry(frame, textvariable=pool_var, width=32)
    pool_entry.grid(column=1, row=6, sticky="ew", pady=3)
    if name_locked:
        db_entry.state(["disabled"])
        pool_entry.state(["disabled"])
    db_note = (
        "Locked — the server has already started under this backend."
        if name_locked
        else "Blank = built-in SQLite (personal / small). PostgreSQL URL "
        "(postgresql://user:pass@host:5432/neuron) = medium / large."
    )
    ttk.Label(frame, text=db_note, wraplength=380, foreground="#7C8896").grid(
        column=0, row=7, columnspan=2, sticky="w", pady=(0, 6)
    )

    ttk.Label(frame, text="Data folder").grid(column=0, row=8, sticky="w")
    ttk.Label(frame, text=str(config.data_path), foreground="#7C8896", wraplength=300).grid(
        column=1, row=8, sticky="w", pady=3
    )

    def _save() -> None:
        if not name_locked:
            err = validate_server_name(name_var.get()) or validate_database_url(db_var.get())
            if err:
                messagebox.showerror("Invalid setting", err)
                return
        result["value"] = updated_config(
            config,
            server_name=config.server_name if name_locked else name_var.get(),
            bind_host=host_var.get(),
            bind_port=port_var.get(),
            database_url=config.database_url if name_locked else db_var.get(),
            db_pool_size=config.db_pool_size if name_locked else pool_var.get(),
        )
        root.destroy()

    def _cancel() -> None:
        result["value"] = None
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(column=0, row=9, columnspan=2, pady=(14, 0), sticky="e")
    ttk.Button(buttons, text="Cancel", command=_cancel).grid(column=0, row=0, padx=6)
    ttk.Button(buttons, text="Save", command=_save).grid(column=1, row=0)

    root.protocol("WM_DELETE_WINDOW", _cancel)
    name_entry.focus_set()
    root.mainloop()
    return result["value"]
