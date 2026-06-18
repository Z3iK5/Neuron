# Contributing

Thanks for your interest in Neuron! This is a Python 3.11+ project; all the code lives
under `neuron/`.

## Development setup

```bash
cd neuron
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"        # all packages + lint/type/test tooling
```

## Checks

CI runs these three on every change under `neuron/**`; run them locally before pushing:

```bash
ruff check .     # lint + import order
mypy             # static type checking (strict)
pytest           # unit tests
```

Integration tests under `neuron/tests/integration/` auto-skip unless
`NEURON_HOMESERVER_URL` and `NEURON_HOMESERVER_ADMIN_TOKEN` point at a running server.

## Guidelines

- Keep changes small and focused; match the style and comment density of the
  surrounding code.
- Add or update tests for behavior changes — the test suite is the safety net.
- New configuration belongs in the relevant `config.py`; document user-facing options in
  [`docs/configuration.md`](docs/configuration.md).

## License

By contributing you agree that your contributions are licensed under the Apache License
2.0 (see [`LICENSE`](LICENSE)).
