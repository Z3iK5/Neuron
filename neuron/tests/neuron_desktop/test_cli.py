# SPDX-License-Identifier: Apache-2.0
"""Tests for the desktop CLI plumbing and the frozen-app server re-exec (D3)."""

from __future__ import annotations

import sys
from collections.abc import Sequence

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
