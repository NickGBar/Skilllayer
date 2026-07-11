#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"

cd "${REPO_ROOT}"

echo "SkillLayer installer"
echo "  repo: ${REPO_ROOT}"
echo "  sudo: not required"

if [[ ! -f "pyproject.toml" ]]; then
  echo "error: pyproject.toml not found. Run this script from a SkillLayer checkout." >&2
  exit 1
fi

# Reject early if the system python3 is too old, before any venv is created.
if ! command -v python3 &>/dev/null; then
  echo "error: python3 was not found. SkillLayer requires Python 3.10 or newer." >&2
  exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    SYS_PY="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
    echo "error: Python 3.10 or newer is required. Found: ${SYS_PY}" >&2
    echo >&2
    echo "  Install a supported Python version, then re-run this script:" >&2
    echo "    macOS:          brew install python@3.12" >&2
    echo "    Ubuntu/Debian:  sudo apt install python3.12" >&2
    echo "    Any platform:   https://www.python.org/downloads/" >&2
    echo >&2
    echo "  If python3.12 (or newer) is already installed but not the default:" >&2
    echo "    python3.12 -m venv .venv && source .venv/bin/activate" >&2
    echo "    python -m pip install '.[mcp]' --no-build-isolation" >&2
    exit 1
fi

if [[ -e "${VENV_DIR}" && ! -d "${VENV_DIR}" ]]; then
  echo "error: .venv exists but is not a directory. Refusing to overwrite it." >&2
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating virtual environment at .venv"
  python3 -m venv "${VENV_DIR}"
else
  echo "Using existing virtual environment at .venv"
fi

PYTHON="${VENV_DIR}/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "error: ${PYTHON} is not executable. Recreate .venv and try again." >&2
  exit 1
fi

PY_VERSION="$("${PYTHON}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
echo "Detected Python: ${PY_VERSION}"
if ! "${PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "INSTALL FAILED" >&2
  echo "Reason: SkillLayer requires Python >=3.10. Detected Python: ${PY_VERSION}" >&2
  exit 1
fi

echo "Upgrading pip, setuptools, and wheel inside .venv"
if ! "${PYTHON}" -m pip install --upgrade pip setuptools wheel; then
  echo "INSTALL FAILED" >&2
  echo "Reason: failed to upgrade pip, setuptools, and wheel." >&2
  exit 1
fi

echo "Installing SkillLayer with the required MCP runtime extra"
if ! "${PYTHON}" -m pip install ".[mcp]" --no-build-isolation; then
  echo "INSTALL FAILED" >&2
  echo "Reason: failed to install the required MCP runtime extra." >&2
  exit 1
fi

echo "Running installed-runtime verification"
if ! "${PYTHON}" -m skilllayer doctor --json; then
  echo "INSTALL FAILED" >&2
  echo "Reason: doctor failed after installation." >&2
  exit 1
fi

echo
echo "SkillLayer install validation completed."
echo "Next steps:"
echo "  ${PYTHON} -m skilllayer doctor --json"
echo "  ${PYTHON} -m skilllayer workflows --json"
echo "  ${PYTHON} -m skilllayer mcp-config --output skilllayer-mcp.json"
