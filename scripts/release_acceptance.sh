#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON is missing; run scripts/install.sh first." >&2
  exit 1
fi

cd "$ROOT"
exec "$PYTHON" -m pytest -q tests/test_release_install_mcp.py
