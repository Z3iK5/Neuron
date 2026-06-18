# NEURON brand assets

Concept 1 — **Neural Shield**: a hexagon of six nodes wired to a central node.

| File | Use |
|---|---|
| `neuron-mark.svg` / `neuron-mark-white.svg` | the bare mark (navy / white) |
| `neuron-icon.png` (1024) · `neuron-icon-512.png` | app icon — mark on a navy squircle |
| `neuron-social.svg` / `neuron-social.png` (1280×640) | repository social preview / banner |
| `../packaging/icons/neuron.ico` | Windows / installer icon |

## Single source of truth

Everything is generated from **`src/neuron_core/branding.py`** — the mark geometry,
palette (Navy `#1C3D5F`, Deep `#0E2740`, Paper `#ECEAE4`), and type (Cinzel
wordmark + Jost UI). The same module powers the homeserver landing page
(`GET /`), the admin console (`neuron_console`), and the desktop tray/dock icon
(`neuron_desktop.icon`), so the brand stays identical across surfaces.

Regenerate these files after changing the brand:

```bash
python packaging/make_brand_assets.py
```

> The SVGs are the true-brand source (they use the Cinzel/Jost web fonts). The PNG
> banner is a raster export for GitHub's social preview (which must be raster) and
> uses a bundled serif as a Cinzel stand-in. On a machine with Cinzel installed,
> export the PNG from `neuron-social.svg` for an exact wordmark.
