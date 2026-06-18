<div align="center">

<img src="assets/brand/neuron-icon.png" alt="NEURON" width="96" height="96">

# NEURON — project sources

</div>

This directory holds the Neuron Python project (all packages, tests, packaging, and the
local dev stack). For what Neuron is, how to install it, and how to run each component,
start at the **[repository README](../README.md)** and the **[docs/](../docs/)** folder.

## Layout

```
neuron/
├── src/
│   ├── neuron_server/      # the Matrix homeserver
│   ├── neuron_core/        # shared library + brand single-source-of-truth
│   ├── neuron_console/     # web admin console
│   ├── neuron_supervisor/  # moderation bot
│   ├── neuron_auditor/     # audit bot
│   ├── neuron_crypto/      # E2EE (Megolm/Olm) decryption helpers
│   └── neuron_desktop/     # desktop app: supervisor, first-run, tray
├── tests/                  # unit tests (+ auto-skipping integration tests)
├── packaging/              # PyInstaller spec, installer scripts, icons
├── deploy/compose/         # optional local stack for interop testing
├── scripts/                # dev helpers
└── pyproject.toml
```

## Develop

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
ruff check . && mypy && pytest
```

See **[../CONTRIBUTING.md](../CONTRIBUTING.md)** for details.
