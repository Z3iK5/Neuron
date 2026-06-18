# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``neuron-server`` entry point dispatch (serve / doctor / argv)."""

from __future__ import annotations

import neuron_server.__main__ as server_main


def test_main_empty_argv_serves(monkeypatch) -> None:
    # No subcommand → the default "serve" path. (The desktop app calls main([])
    # when re-execing its frozen server child, so this must not raise.)
    served: list[object] = []
    monkeypatch.setattr(server_main, "_serve", lambda settings: served.append(settings))
    monkeypatch.setattr(
        server_main, "_doctor", lambda *a, **k: (_ for _ in ()).throw(AssertionError("doctor"))
    )
    server_main.main([])
    assert len(served) == 1


def test_main_explicit_serve_subcommand(monkeypatch) -> None:
    served: list[object] = []
    monkeypatch.setattr(server_main, "_serve", lambda settings: served.append(settings))
    server_main.main(["serve"])
    assert len(served) == 1


def test_main_routes_doctor_and_returns_its_exit_code(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_doctor(settings: object, *, offline: bool, strict: bool) -> int:
        seen.update(offline=offline, strict=strict)
        return 3

    monkeypatch.setattr(server_main, "_doctor", fake_doctor)
    monkeypatch.setattr(
        server_main, "_serve", lambda settings: (_ for _ in ()).throw(AssertionError("serve"))
    )
    try:
        server_main.main(["doctor", "--offline", "--strict"])
    except SystemExit as exc:
        assert exc.code == 3
    else:  # pragma: no cover - the doctor path must exit
        raise AssertionError("doctor path should raise SystemExit")
    assert seen == {"offline": True, "strict": True}
