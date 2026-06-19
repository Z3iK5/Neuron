# SPDX-License-Identifier: Apache-2.0
"""Tests for structured logging setup, including the windowed/frozen edge case.

A PyInstaller windowed build (``console=False``) on Windows has ``sys.stdout`` and
``sys.stderr`` set to ``None``. Configuring logging — and emitting a record — must
not crash in that case (it previously would, via a ``StreamHandler(None)``).
"""

from __future__ import annotations

import logging
import sys

import pytest

from neuron_core.logging import configure_logging, get_logger


def test_configure_logging_survives_none_std_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        # Simulate a windowed frozen app where both standard streams are None.
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        # Neither configuration nor emission may raise.
        configure_logging(level="INFO", fmt="json")
        assert root.handlers, "a fallback handler should be installed"
        get_logger("neuron.test").info("hello", extra={"shape": "round"})

        configure_logging(level="DEBUG", fmt="console")
        get_logger("neuron.test").warning("still fine")
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_uses_stderr_when_stdout_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        monkeypatch.setattr(sys, "stdout", None)
        # stderr is still a real stream; the handler should fall back to it.
        configure_logging(level="INFO", fmt="json")
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stderr
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
