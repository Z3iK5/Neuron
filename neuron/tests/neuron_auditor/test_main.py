# SPDX-License-Identifier: Apache-2.0
"""Tests for the auditor CLI wiring in ``neuron_auditor.__main__``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from neuron_auditor.__main__ import _build_decryptor
from neuron_auditor.config import AuditorSettings
from neuron_crypto.manager import E2EEManager


class FakeE2EEClient:
    """Just enough of MatrixClient for _build_decryptor's full-E2EE path."""

    async def whoami(self) -> dict[str, Any]:
        return {"user_id": "@bot:hs", "device_id": "BOTDEV"}

    async def keys_upload(
        self,
        *,
        device_keys: dict[str, Any] | None = None,
        one_time_keys: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"one_time_key_counts": {}}


async def test_one_time_keys_setting_feeds_replenish_target(tmp_path: Path) -> None:
    """Regression: auditor_e2e_one_time_keys must set E2EEManager's replenish
    target, not just the initial upload (it used to fall back to the default 50)."""
    settings = AuditorSettings(
        auditor_e2e_device_store=str(tmp_path / "device.json"),
        auditor_e2e_one_time_keys=7,
    )
    decryptor = await _build_decryptor(FakeE2EEClient(), settings)  # type: ignore[arg-type]

    assert isinstance(decryptor, E2EEManager)
    # With the server reporting zero published keys, replenishment must top up
    # to the configured target (7), not the hardcoded default (50).
    keys = decryptor.maybe_generate_one_time_keys(0)
    assert keys is not None
    assert len(keys) == 7
