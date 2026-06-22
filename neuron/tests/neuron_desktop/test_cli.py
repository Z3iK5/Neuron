# SPDX-License-Identifier: Apache-2.0
"""Tests for the desktop CLI plumbing and the frozen-app server re-exec (D3)."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from neuron_desktop import cli, paths, process, setup
from neuron_desktop import config as config_module
from neuron_desktop.config import DesktopConfig


def test_default_server_command_normal_vs_frozen(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert process.default_server_command() == [sys.executable, "-m", "neuron_server"]

    # A PyInstaller bundle re-execs itself instead of a separate interpreter.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Neuron.app/Neuron")
    assert process.default_server_command() == ["/Applications/Neuron.app/Neuron", "_server"]


def test_cli_internal_server_command_runs_homeserver(monkeypatch) -> None:
    import neuron_server.__main__ as server_main

    # Capture the argv the desktop layer hands to the homeserver entry point: it
    # must NOT contain our internal "_server" token (which neuron_server's parser
    # would reject), so an empty list is passed and the server defaults to serve.
    seen: list[Sequence[str] | None] = []
    monkeypatch.setattr(server_main, "main", lambda argv=None: seen.append(argv))
    assert cli.main(["_server"]) == 0
    assert seen == [[]]


def test_cli_where_prints_paths(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("NEURON_DATA_DIR", str(tmp_path))
    assert cli.main(["where"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path) in out and "config file" in out


def _existing_install(base: Path, *, version: str = "0.0.0-old") -> None:
    config_module.save(DesktopConfig("old.server", str(base), "admin"), paths.config_path(base))
    paths.database_path(base).write_bytes(b"db")
    setup.welcome_path(base).write_text("welcome", encoding="utf-8")
    setup.record_version(base, version)


def test_configured_upgrades_existing_install_when_headless(tmp_path: Path) -> None:
    # No tty in tests -> the default chooser upgrades (never auto-erases).
    _existing_install(tmp_path)
    config = cli._configured(tmp_path)
    assert config.server_name == "old.server"  # data preserved
    assert paths.database_path(tmp_path).exists()
    assert not setup.welcome_path(tmp_path).exists()  # pruned
    assert setup.installed_version(tmp_path) == setup.current_app_version()  # re-stamped


def test_configured_aborts_when_install_choice_cancelled(monkeypatch, tmp_path: Path) -> None:
    _existing_install(tmp_path)
    monkeypatch.setattr(setup, "default_install_chooser", lambda _e: setup.INSTALL_CANCEL)
    with pytest.raises(SystemExit):
        cli._configured(tmp_path)
    # Nothing was removed.
    assert paths.config_path(tmp_path).exists()
    assert setup.welcome_path(tmp_path).exists()
