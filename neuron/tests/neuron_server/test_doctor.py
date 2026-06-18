# SPDX-License-Identifier: Apache-2.0
"""Tests for ``neuron-server doctor`` (preflight / health checks)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI

from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings
from neuron_server.doctor import (
    CheckResult,
    Status,
    exit_code,
    format_report,
    run_checks,
)


def _settings(tmp_path: Path, **overrides: object) -> NeuronServerSettings:
    base: dict[str, object] = {
        "name": "chat.example.org",
        "database_url": f"sqlite:///{tmp_path / 'hs.db'}",
        "media_store_path": str(tmp_path / "media"),
        "public_base_url": "https://chat.example.org",
        "registration_enabled": False,
        "admin_users": "admin",
    }
    base.update(overrides)
    return NeuronServerSettings(**base)  # type: ignore[arg-type]


def _by_name(results: list[CheckResult], name: str) -> CheckResult:
    return next(r for r in results if r.name == name)


async def _init_db(settings: NeuronServerSettings) -> FastAPI:
    """Run the app lifespan once so the DB is migrated and the key persisted."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        pass
    return app


# --- config checks ----------------------------------------------------------


async def test_clean_config_is_all_ok(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _init_db(settings)
    results = await run_checks(settings, offline=True)
    assert all(r.status is Status.OK for r in results), [
        (r.name, r.detail) for r in results if r.status is not Status.OK
    ]
    assert exit_code(results, strict=False) == 0


async def test_dev_defaults_warn_but_do_not_fail(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local",
        database_url=f"sqlite:///{tmp_path / 'hs.db'}",
        media_store_path=str(tmp_path / "media"),
        public_base_url="http://localhost:8008",
    )
    results = await run_checks(settings, offline=True)
    assert _by_name(results, "server name").status is Status.WARN
    assert _by_name(results, "public base URL").status is Status.WARN
    assert _by_name(results, "registration").status is Status.WARN  # open by default
    assert _by_name(results, "admin users").status is Status.WARN
    assert not any(r.status is Status.FAIL for r in results)
    assert exit_code(results, strict=False) == 0
    # --strict turns the warnings into a non-zero exit.
    assert exit_code(results, strict=True) == 1


async def test_database_identity_mismatch_fails(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _init_db(settings)  # records server_name = chat.example.org
    wrong = _settings(tmp_path, name="evil.example.org")
    results = await run_checks(wrong, offline=True)
    identity = _by_name(results, "server identity")
    assert identity.status is Status.FAIL
    assert exit_code(results, strict=False) == 1


async def test_invalid_public_base_url_fails(tmp_path: Path) -> None:
    settings = _settings(tmp_path, public_base_url="not-a-url")
    results = await run_checks(settings, offline=True)
    assert _by_name(results, "public base URL").status is Status.FAIL


async def test_media_store_not_writable_fails(tmp_path: Path) -> None:
    # A regular file where a directory is expected cannot be a media store.
    blocker = tmp_path / "media"
    blocker.write_text("not a directory")
    settings = _settings(tmp_path)
    results = await run_checks(settings, offline=True)
    assert _by_name(results, "media store").status is Status.FAIL


async def test_registration_closed_and_admins_ok(tmp_path: Path) -> None:
    settings = _settings(tmp_path)  # closed + admin set
    results = await run_checks(settings, offline=True)
    assert _by_name(results, "registration").status is Status.OK
    assert _by_name(results, "admin users").status is Status.OK


async def test_signing_key_file_states(tmp_path: Path) -> None:
    key_path = tmp_path / "signing.key"
    settings = _settings(tmp_path, signing_key_path=str(key_path))

    # Missing file -> OK (will be generated).
    missing = await run_checks(settings, offline=True)
    assert _by_name(missing, "signing key").status is Status.OK

    # Garbage file -> FAIL.
    key_path.write_text("this is not a valid signing key")
    bad = await run_checks(settings, offline=True)
    assert _by_name(bad, "signing key").status is Status.FAIL

    # A valid key -> OK and reports the key id.
    key_path.write_text("ed25519 abc " + "A" * 43 + "\n")
    good = await run_checks(settings, offline=True)
    sk = _by_name(good, "signing key")
    assert sk.status is Status.OK and "ed25519:abc" in sk.detail


async def test_unreachable_database_fails(tmp_path: Path) -> None:
    # Point at a directory that does not exist so the sqlite file cannot be opened.
    settings = _settings(tmp_path, database_url=f"sqlite:///{tmp_path / 'no' / 'dir' / 'hs.db'}")
    results = await run_checks(settings, offline=True)
    assert _by_name(results, "database").status is Status.FAIL


# --- network checks (via in-process seams) ----------------------------------


async def test_online_checks_against_in_process_server(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        http_client = httpx.AsyncClient(transport=transport, base_url=settings.public_base_url)

        def fed_open(_destination: str) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=transport, base_url="http://fed")

        try:
            results = await run_checks(
                settings, offline=False, http_client=http_client, fed_open_client=fed_open
            )
        finally:
            await http_client.aclose()

    assert _by_name(results, "client discovery").status is Status.OK
    assert _by_name(results, "federation routing").status is Status.OK
    # The server self-signs the key document we fetch back over federation.
    assert _by_name(results, "federation reachability").status is Status.OK


# --- reporting / CLI --------------------------------------------------------


def test_format_and_exit_code() -> None:
    results = [
        CheckResult("a", Status.OK, "fine"),
        CheckResult("b", Status.WARN, "careful"),
        CheckResult("c", Status.FAIL, "broken"),
    ]
    report = format_report(results, color=False)
    assert "1 ok · 1 warning(s) · 1 failure(s)" in report
    assert "broken" in report
    assert exit_code(results, strict=False) == 1
    # Without the failure, only warnings remain.
    healthy = results[:2]
    assert exit_code(healthy, strict=False) == 0
    assert exit_code(healthy, strict=True) == 1


def test_cli_doctor_offline_runs(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "NEURON_SERVER_NAME": "chat.example.org",
        "NEURON_SERVER_DATABASE_URL": f"sqlite:///{tmp_path / 'hs.db'}",
        "NEURON_SERVER_MEDIA_STORE_PATH": str(tmp_path / "media"),
        "NEURON_SERVER_PUBLIC_BASE_URL": "https://chat.example.org",
        "NEURON_SERVER_REGISTRATION_ENABLED": "false",
        "NEURON_SERVER_ADMIN_USERS": "admin",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "neuron_server", "doctor", "--offline"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "server name" in proc.stdout
    assert "failure(s)" in proc.stdout
