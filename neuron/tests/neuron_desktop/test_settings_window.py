# SPDX-License-Identifier: Apache-2.0
"""Tests for the native settings window's pure logic (no display required).

The tkinter form itself needs a display, so only the GUI-agnostic helpers are unit
tested here; the window is exercised end-to-end on the built app during release
verification.
"""

from __future__ import annotations

from pathlib import Path

from neuron_desktop import settings_window as sw
from neuron_desktop.config import DesktopConfig


def _cfg(tmp_path: Path) -> DesktopConfig:
    return DesktopConfig("old.name", str(tmp_path), "admin", bind_host="127.0.0.1", bind_port=8008)


def test_updated_config_applies_inputs(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    updated = sw.updated_config(
        cfg, server_name="  new.example  ", bind_host="0.0.0.0", bind_port="9000"
    )
    assert updated.server_name == "new.example"
    assert updated.bind_host == "0.0.0.0"
    assert updated.bind_port == 9000
    # The rest of the config is preserved.
    assert updated.admin_username == cfg.admin_username
    assert updated.data_dir == cfg.data_dir


def test_updated_config_ignores_blank_or_invalid(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    updated = sw.updated_config(cfg, server_name="   ", bind_host="", bind_port="not-a-number")
    assert updated.server_name == "old.name"
    assert updated.bind_host == "127.0.0.1"
    assert updated.bind_port == 8008


def test_validate_server_name() -> None:
    assert sw.validate_server_name("chat.example.org") is None
    assert sw.validate_server_name("") is not None
    assert sw.validate_server_name("   ") is not None
    assert sw.validate_server_name("has space") is not None
    assert sw.validate_server_name("has/slash") is not None


def test_identity_committed_tracks_database_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert sw.identity_committed(cfg) is False
    (tmp_path / "homeserver.db").write_text("", encoding="utf-8")
    assert sw.identity_committed(cfg) is True
