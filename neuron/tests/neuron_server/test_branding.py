# SPDX-License-Identifier: Apache-2.0
"""Tests for the NEURON brand assets and the homeserver landing page."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from neuron_core import branding
from neuron_desktop.icon import render_icon
from neuron_server.app import create_app
from neuron_server.config import NeuronServerSettings


def test_mark_svg_is_well_formed() -> None:
    svg = branding.mark_svg(branding.NAVY)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "viewBox=\"0 0 200 200\"" in svg
    assert svg.count("<circle") == 7  # six outer nodes + the centre
    assert svg.count("<line") == 6  # six spokes
    assert branding.NAVY in svg


def test_favicon_data_uri() -> None:
    uri = branding.favicon_data_uri()
    assert uri.startswith("data:image/svg+xml,")


def test_landing_page_contains_brand() -> None:
    html = branding.landing_page_html("neuron.local")
    assert "NEURON" in html
    assert branding.TAGLINE in html
    assert "neuron.local" in html
    assert branding.DEEP in html  # dark brand background
    assert "<svg" in html  # the mark is inlined


def test_render_icon_dimensions() -> None:
    icon = render_icon(128)
    assert icon.size == (128, 128)
    assert icon.mode == "RGBA"
    # Transparent variant leaves corners clear (squircle/no background).
    transparent = render_icon(128, background=None)
    assert transparent.getpixel((0, 0))[3] == 0


def test_server_serves_landing_and_favicon(tmp_path: Path) -> None:
    settings = NeuronServerSettings(
        name="neuron.local", database_url=f"sqlite:///{tmp_path / 'hs.db'}"
    )
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "NEURON" in page.text and "neuron.local" in page.text

        favicon = client.get("/favicon.svg")
        assert favicon.status_code == 200
        assert favicon.headers["content-type"].startswith("image/svg+xml")
