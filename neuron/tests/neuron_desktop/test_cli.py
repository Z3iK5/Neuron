# SPDX-License-Identifier: Apache-2.0
"""Tests for the desktop CLI plumbing and the frozen-app server re-exec (D3)."""

from __future__ import annotations

import sys

from neuron_desktop import cli, process


def test_default_server_command_normal_vs_frozen(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert process.default_server_command() == [sys.executable, "-m", "neuron_server"]

    # A PyInstaller bundle re-execs itself instead of a separate interpreter.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Neuron.app/Neuron")
    assert process.default_server_command() == ["/Applications/Neuron.app/Neuron", "_server"]


def test_cli_internal_server_command_runs_homeserver(monkeypatch) -> None:
    import neuron_server.__main__ as server_main

    called: list[bool] = []
    monkeypatch.setattr(server_main, "main", lambda: called.append(True))
    assert cli.main(["_server"]) == 0
    assert called == [True]


def test_cli_where_prints_paths(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("NEURON_DATA_DIR", str(tmp_path))
    assert cli.main(["where"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path) in out and "config file" in out
