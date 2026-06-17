#!/usr/bin/env bash
# Set up a local Neuron development environment:
#   - create a Python virtualenv in neuron/.venv
#   - install Neuron with its dev tools
#   - run the linter, type checker, and unit tests
#
# Usage (from anywhere):  bash neuron/scripts/dev-setup.sh
set -euo pipefail

# Resolve the neuron/ directory regardless of where this is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEURON_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$NEURON_DIR"

echo "==> Creating virtualenv (.venv) ..."
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate

echo "==> Installing Neuron (editable) with dev dependencies ..."
python -m pip install --upgrade pip >/dev/null
pip install -e ".[dev]"

echo "==> Linting (ruff) ..."
ruff check .

echo "==> Type checking (mypy) ..."
mypy

echo "==> Running unit tests (pytest) ..."
pytest -q

echo
echo "All good. Activate the environment with:  . neuron/.venv/bin/activate"
echo "To start a local Synapse to test against, see neuron/deploy/compose/README.md"
