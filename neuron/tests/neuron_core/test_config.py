"""Tests for neuron_core.config — configuration loading from the environment."""

from __future__ import annotations

import pytest

from neuron_core.config import NeuronCoreSettings


def test_defaults_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure no NEURON_* vars and no .env leak into this test.
    for key in list(__import__("os").environ):
        if key.startswith("NEURON_"):
            monkeypatch.delenv(key, raising=False)

    settings = NeuronCoreSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.synapse_base_url == "http://localhost:8008"
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.has_admin_token() is False


def test_reads_prefixed_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEURON_SYNAPSE_BASE_URL", "https://matrix.example.org")
    monkeypatch.setenv("NEURON_SYNAPSE_ADMIN_TOKEN", "syt_secret_token")
    monkeypatch.setenv("NEURON_LOG_LEVEL", "DEBUG")

    settings = NeuronCoreSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.synapse_base_url == "https://matrix.example.org"
    assert settings.log_level == "DEBUG"
    assert settings.has_admin_token() is True
    # The token is a SecretStr: its real value is only available via get_secret_value().
    assert settings.synapse_admin_token.get_secret_value() == "syt_secret_token"


def test_secret_is_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEURON_SYNAPSE_ADMIN_TOKEN", "syt_super_secret")
    settings = NeuronCoreSettings(_env_file=None)  # type: ignore[call-arg]
    # Pydantic's SecretStr hides the value in string representations.
    assert "syt_super_secret" not in repr(settings)
