# SPDX-License-Identifier: Apache-2.0
"""The desktop config file and its mapping onto server settings.

A small JSON file (``config.json`` in the data dir) records the user's first-run
choices. :meth:`DesktopConfig.to_server_settings` derives a fully-specified
:class:`NeuronServerSettings` from it — pointing the database, media, and signing
key at the data directory — so the desktop app never depends on environment
configuration.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from neuron_desktop import paths
from neuron_server.config import NeuronServerSettings

# Bind addresses that should be shown to the user as "localhost" in the browser.
_LOOPBACK_BINDS = {"0.0.0.0", "::", "", "127.0.0.1", "::1"}


@dataclass
class DesktopConfig:
    """The user's persisted first-run choices."""

    server_name: str
    data_dir: str
    admin_username: str
    bind_host: str = "127.0.0.1"
    bind_port: int = 8008
    public_base_url: str = ""
    # First-run (non-interactive) flow: the first account created in the browser
    # becomes the admin, so there's no pre-created default password.
    first_user_admin: bool = False

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    def console_url(self) -> str:
        """The URL to open in a browser for the admin console."""
        if self.public_base_url:
            return self.public_base_url
        host = "localhost" if self.bind_host in _LOOPBACK_BINDS else self.bind_host
        return f"http://{host}:{self.bind_port}"

    def to_server_settings(self) -> NeuronServerSettings:
        base = self.data_path
        return NeuronServerSettings(
            name=self.server_name,
            public_base_url=self.console_url(),
            database_url=f"sqlite:///{paths.database_path(base)}",
            media_store_path=str(paths.media_path(base)),
            signing_key_path=str(paths.signing_key_path(base)),
            admin_users=self.admin_username,
            first_user_admin=self.first_user_admin,
            bind_host=self.bind_host,
            bind_port=self.bind_port,
        )


def load(config_file: Path) -> DesktopConfig:
    data = json.loads(config_file.read_text(encoding="utf-8"))
    return DesktopConfig(**data)


def save(config: DesktopConfig, config_file: Path) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
